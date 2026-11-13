# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class FleetRepair(models.Model):
    _inherit = 'fleet.repair'

    appointment_id = fields.Many2one(
        'business.appointment',
        string='Origin Workshop Appointment',
        readonly=True,
        copy=False,
        index=True,
        ondelete='restrict',
        tracking=True,
        help='Confirmed appointment that originated this repair order (unique 1:1 relationship).'
    )
    is_appointment_origin = fields.Boolean(
        string='Origin in Appointment',
        default=False,
        readonly=True,
        copy=False,
        index=True,
        help='Indicates whether this job was generated from a previous workshop booking (Pre-arrival).'
    )
    reception_state = fields.Selection(
        selection=[
            ('booked', 'Booked / Awaiting Reception'),
            ('received', 'Physically Received'),
            ('no_show', 'Missed (No-Show)'),
            ('cancelled', 'Cancelled'),
        ],
        string='Reception Status',
        default='received',
        required=True,
        copy=False,
        index=True,
        tracking=True,
        help='Distinguishes pre-arrival orders generated from appointments (Booked / Awaiting Reception) from physically received vehicles.'
    )
    is_pending_reception = fields.Boolean(
        string='Awaiting Physical Reception',
        compute='_compute_is_pending_reception',
        store=True,
        index=True,
    )
    planned_start_datetime = fields.Datetime(
        string='Planned Start (Appointment)',
        readonly=True,
        copy=False,
        tracking=True,
    )
    planned_end_datetime = fields.Datetime(
        string='Planned End (Appointment)',
        readonly=True,
        copy=False,
        tracking=True,
    )
    actual_receipt_datetime = fields.Datetime(
        string='Verified Actual Arrival',
        readonly=True,
        copy=False,
        tracking=True,
        help='Actual date and time when the vehicle was physically received at reception.'
    )
    reported_odometer = fields.Float(
        string='Reported Odometer (Appointment)',
        readonly=True,
        copy=False,
        tracking=True,
        help='Mileage/odometer reported by customer when booking the appointment.'
    )
    verified_odometer = fields.Float(
        string='Verified Odometer (Reception)',
        copy=False,
        tracking=True,
        help='Mileage/odometer physically verified when receiving the vehicle at the workshop.'
    )
    odometer_unit = fields.Selection(
        related='vehicle_id.odometer_unit',
        string='Odometer Unit',
        readonly=True,
    )
    is_odometer_verified = fields.Boolean(
        string='Odometer Verified',
        default=False,
        readonly=True,
        copy=False,
        tracking=True,
    )

    _sql_constraints = [
        (
            'unique_appointment_id',
            'unique(appointment_id)',
            'Concurrency error: A service repair order (SR) linked exclusively to this appointment already exists.'
        ),
    ]

    @api.depends('is_appointment_origin', 'reception_state', 'state')
    def _compute_is_pending_reception(self):
        for rec in self:
            rec.is_pending_reception = bool(
                rec.is_appointment_origin
                and rec.reception_state == 'booked'
                and rec.state == 'draft'
            )

    def _is_progressed_sr(self):
        self.ensure_one()
        has_tasks = bool(getattr(self, 'tasks_ids', False))
        return bool(
            self.state not in ('draft', 'cancel')
            or (self.is_appointment_origin and self.reception_state == 'received')
            or self.diagnose_id
            or self.workorder_id
            or self.sale_order_id
            or has_tasks
        )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('appointment_id') or vals.get('is_appointment_origin'):
                vals['is_appointment_origin'] = True
                vals.setdefault('reception_state', 'booked')
                if vals.get('current_odometer') and not vals.get('reported_odometer'):
                    vals['reported_odometer'] = vals['current_odometer']
            else:
                vals.setdefault('reception_state', 'received')
                if not vals.get('actual_receipt_datetime') and vals.get('receipt_date_time'):
                    vals['actual_receipt_datetime'] = vals['receipt_date_time']
                if vals.get('current_odometer') and not vals.get('verified_odometer'):
                    vals['verified_odometer'] = vals['current_odometer']
                    vals.setdefault('is_odometer_verified', True)
        return super(FleetRepair, self).create(vals_list)

    def write(self, vals):
        if 'appointment_id' in vals and not self.env.context.get('allow_appointment_link_sync'):
            for rec in self:
                if rec.appointment_id and vals.get('appointment_id') != rec.appointment_id.id:
                    raise UserError(_(
                        "It is not permitted to reassign or unlink the Origin Appointment (%s) of a Service Repair Order (SR)."
                    ) % rec.appointment_id.display_name)

        if 'current_odometer' in vals and not self.env.context.get('confirming_physical_reception'):
            pre_arrival_recs = self.filtered(
                lambda r: r.is_appointment_origin and r.reception_state != 'received'
            )
            other_recs = self - pre_arrival_recs
            if pre_arrival_recs:
                new_odom = vals['current_odometer']
                if new_odom <= 0:
                    raise ValidationError(_("The reported odometer must be greater than zero."))
                vals_no_odom = dict(vals)
                vals_no_odom.pop('current_odometer')
                vals_no_odom.setdefault('reported_odometer', new_odom)
                res = True
                if vals_no_odom:
                    res = super(FleetRepair, pre_arrival_recs).write(vals_no_odom)
                for rec in pre_arrival_recs:
                    self.env.cr.execute(
                        "UPDATE fleet_repair SET current_odometer = %s, reported_odometer = %s WHERE id = %s",
                        (new_odom, vals_no_odom.get('reported_odometer', new_odom), rec.id),
                    )
                pre_arrival_recs.invalidate_recordset(['current_odometer', 'reported_odometer'])
                if other_recs:
                    res = super(FleetRepair, other_recs).write(vals) and res
                return res

        return super(FleetRepair, self).write(vals)

    def unlink(self):
        for rec in self:
            if rec.appointment_id and not self.env.context.get('force_unlink_appointment_sr'):
                raise UserError(_(
                    "Cannot delete Repair Order '%s' because it is linked to appointment '%s'. Please use the audited cancellation flow."
                ) % (rec.sequence or rec.name, rec.appointment_id.name or rec.appointment_id.id))
        return super(FleetRepair, self).unlink()

    def action_confirm_physical_reception(self, actual_arrival=None, verified_odometer=None):
        for rec in self:
            if rec.state == 'cancel' or rec.reception_state == 'cancelled':
                raise UserError(_(
                    "Cannot confirm physical reception of a cancelled Repair Order (%s)."
                ) % (rec.sequence or rec.name))

            odom = (
                verified_odometer
                if verified_odometer is not None
                else (rec.verified_odometer or rec.current_odometer or rec.reported_odometer)
            )
            if not odom or odom <= 0:
                raise ValidationError(_(
                    "You must enter a valid Verified Odometer (greater than 0) to confirm physical reception."
                ))
            if rec.previous_odometer and odom < rec.previous_odometer and not self.env.context.get('allow_lower_odometer'):
                raise ValidationError(_(
                    "The verified odometer (%s) cannot be lower than the previously recorded vehicle odometer (%s)."
                ) % (odom, rec.previous_odometer))

            arrival_dt = fields.Datetime.to_datetime(actual_arrival) if actual_arrival else fields.Datetime.now()
            arrival_date = fields.Date.context_today(rec, arrival_dt)
            receiving_emp = self.env['hr.employee'].search([('user_id', '=', self.env.uid)], limit=1)

            update_vals = {
                'reception_state': 'received',
                'actual_receipt_datetime': arrival_dt,
                'receipt_date_time': arrival_dt,
                'receipt_date': arrival_date,
                'verified_odometer': odom,
                'current_odometer': odom,
                'is_odometer_verified': True,
            }
            if receiving_emp:
                update_vals['receiving_employee_id'] = receiving_emp.id

            rec.with_context(confirming_physical_reception=True).write(update_vals)
            if rec.vehicle_id and odom > 0:
                rec.vehicle_id.sudo().write({'odometer': odom})

            audit_msg = _(
                "Physical reception confirmed for SR %(sr)s (Appointment %(app)s). "
                "Actual arrival: %(dt)s UTC | Verified odometer: %(odom)s."
            ) % {
                'sr': rec.sequence or rec.name,
                'app': rec.appointment_id.name if rec.appointment_id else _('N/A'),
                'dt': fields.Datetime.to_string(arrival_dt),
                'odom': odom,
            }
            rec.message_post(body=audit_msg)
            if rec.appointment_id:
                rec.appointment_id.message_post(body=audit_msg)
                if rec.appointment_id.state == 'reserved':
                    rec.appointment_id.with_context(skip_sr_lifecycle_sync=True).write({'state': 'done'})
            _logger.info(
                "SR physical reception confirmed: sr_ref=%s appointment_ref=%s",
                rec.sequence or rec.id,
                rec.appointment_id.name if rec.appointment_id else None,
            )
        return True

    def action_create_fleet_diagnosis(self):
        for rec in self:
            if rec.is_appointment_origin and rec.reception_state != 'received':
                raise UserError(_(
                    "The vehicle for appointment '%s' is still marked as 'Booked / Awaiting Reception'. "
                    "Please confirm Physical Reception and verify odometer before starting diagnosis."
                ) % (rec.appointment_id.name or rec.sequence or rec.name))
        return super(FleetRepair, self).action_create_fleet_diagnosis()

    def action_open_business_appointment(self):
        self.ensure_one()
        if not self.appointment_id:
            return False
        return {
            'name': _('Workshop Appointment'),
            'type': 'ir.actions.act_window',
            'res_model': 'business.appointment',
            'res_id': self.appointment_id.id,
            'view_mode': 'form',
            'target': 'current',
        }
