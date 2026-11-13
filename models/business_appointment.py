# -*- coding: utf-8 -*-
import logging
import uuid
import zlib
from dateutil.relativedelta import relativedelta
from pytz import timezone, UTC

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.osv.expression import AND, OR

_logger = logging.getLogger(__name__)

ARUBA_TZ_NAME = 'America/Aruba'
LOCK_NS_REQUEST_KEY = 104201
LOCK_NS_PRERESERVATION = 104202
LOCK_NS_RESOURCE = 104203
LOCK_NS_VEHICLE = 104204
LOCK_NS_APPOINTMENT = 104205


def _stable_int32_hash(text_value):
    raw = zlib.crc32((text_value or '').encode('utf-8')) & 0xFFFFFFFF
    return raw - 0x100000000 if raw >= 0x80000000 else raw


def _format_aruba_dt(dt_value):
    if not dt_value:
        return ''
    dt_obj = fields.Datetime.to_datetime(dt_value)
    aruba_tz = timezone(ARUBA_TZ_NAME)
    localized = UTC.localize(dt_obj).astimezone(aruba_tz)
    return localized.strftime('%Y-%m-%d %H:%M (%Z)')


class AppointmentContactInfo(models.AbstractModel):
    _inherit = 'appointment.contact.info'

    vehicle_id = fields.Many2one(
        'fleet.vehicle',
        string='Vehicle to Repair',
        domain="[('customer_id', '=', partner_id)]",
        copy=False,
        help='Select the registered fleet vehicle that will enter the workshop.'
    )
    current_odometer = fields.Float(
        string='Reported Odometer',
        copy=False,
        help='Current mileage/odometer reported by the customer when booking.'
    )
    last_odometer = fields.Float(
        string='Last Recorded Odometer',
        compute='_compute_last_odometer',
        readonly=True,
        help='Last master odometer recorded on the vehicle record.'
    )
    odometer_unit = fields.Selection(
        related='vehicle_id.odometer_unit',
        string='Odometer Unit',
        readonly=True,
    )
    expected_delivery_date_time = fields.Datetime(
        string='Estimated Delivery (SR)',
        copy=False,
        help='Estimated delivery date and time for the Service Repair Order (SR).'
    )
    is_authorized_service_contact = fields.Boolean(
        string='Authorized Service Contact',
        default=False,
        copy=False,
        help='Check this option when the person booking/delivering the vehicle is an authorized contact different from the vehicle owner.'
    )
    booking_request_key = fields.Char(
        string='Booking Request Key',
        copy=False,
        index=True,
        help='Stable idempotency identifier to prevent duplicates from retries or double clicks.'
    )

    @api.depends('vehicle_id', 'vehicle_id.odometer')
    def _compute_last_odometer(self):
        for rec in self:
            rec.last_odometer = rec.vehicle_id.odometer if rec.vehicle_id else 0.0

    @api.onchange('partner_id')
    def _onchange_partner_id(self):
        res = super(AppointmentContactInfo, self)._onchange_partner_id()
        if self.vehicle_id and self.vehicle_id.customer_id != self.partner_id:
            self.vehicle_id = False
            if isinstance(res, dict) and 'value' in res:
                res['value']['vehicle_id'] = False
        return res

    @api.onchange('vehicle_id')
    def _onchange_workshop_vehicle_id(self):
        for rec in self:
            if rec.vehicle_id:
                rec.registration_plate = rec.vehicle_id.license_plate or False
                rec.vehicle_model = rec.vehicle_id.model_id.name if rec.vehicle_id.model_id else False
                owner = getattr(rec.vehicle_id, 'customer_id', False) or rec.vehicle_id.driver_id
                if not rec.partner_id and owner:
                    rec.partner_id = owner
                if not rec.current_odometer and rec.vehicle_id.odometer and rec.vehicle_id.odometer > 0:
                    rec.current_odometer = rec.vehicle_id.odometer

    def _return_appointment_values(self, pure_values=False, tosession=False):
        values = super(AppointmentContactInfo, self)._return_appointment_values(
            pure_values=pure_values, tosession=tosession
        )
        values.update({
            'vehicle_id': self.vehicle_id.id if self.vehicle_id else False,
            'current_odometer': self.current_odometer,
            'expected_delivery_date_time': self.expected_delivery_date_time,
            'is_authorized_service_contact': self.is_authorized_service_contact,
            'registration_plate': self.registration_plate,
            'vehicle_model': self.vehicle_model,
            'short_description': getattr(self, 'short_description', False),
            'send_confirmation_email': getattr(self, 'send_confirmation_email', False),
        })
        return values


class ChooseAppointmentCustomer(models.TransientModel):
    _inherit = 'choose.appointment.customer'

    booking_request_key = fields.Char(
        default=lambda self: str(uuid.uuid4()),
    )
    is_workshop_booking = fields.Boolean(
        string='Is Workshop Booking',
        compute='_compute_is_workshop_booking',
    )

    @api.depends(
        'reservation_ids',
        'reservation_ids.service_id',
        'reservation_ids.resource_id',
        'appointment_id',
        'appointment_id.service_id',
        'resource_type_id',
    )
    def _compute_is_workshop_booking(self):
        for wiz in self:
            is_ws = False
            if wiz.appointment_id and wiz.appointment_id._is_workshop_appointment():
                is_ws = True
            elif wiz.reservation_ids:
                for core in wiz.reservation_ids:
                    if core._is_workshop_prereservation():
                        is_ws = True
                        break
            elif wiz.resource_type_id:
                if any(rt.is_workshop_resource_type for rt in wiz.resource_type_id):
                    is_ws = True
            wiz.is_workshop_booking = is_ws

    @api.onchange('reservation_ids', 'appointment_id')
    def _onchange_reservations_expected_delivery(self):
        for wiz in self:
            if not wiz.expected_delivery_date_time:
                target = wiz.reservation_ids[:1] or wiz.appointment_id
                if target and target.datetime_end:
                    lead_hours = getattr(target.service_id, 'default_expected_delivery_lead_hours', 0.0) or 0.0
                    wiz.expected_delivery_date_time = target.datetime_end + relativedelta(hours=lead_hours)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('booking_request_key'):
                vals['booking_request_key'] = str(uuid.uuid4())
        return super(ChooseAppointmentCustomer, self).create(vals_list)


class BusinessAppointmentCore(models.Model):
    _inherit = 'business.appointment.core'

    def _is_workshop_prereservation(self):
        self.ensure_one()
        return bool(
            (self.service_id and (self.service_id.is_workshop_service or self.service_id.repair_service_type_id))
            or (self.resource_id and self.resource_id.resource_type_id.is_workshop_resource_type)
            or self.vehicle_id
        )

    def _prepare_appointment_values(self):
        return self._prepare_vals_for_real_appointment_prereserv()

    def _prepare_vals_for_real_appointment_prereserv(self):
        self.ensure_one()
        if self._is_workshop_prereservation() and not (self.partner_id or self.reservation_group_id.partner_id):
            raise ValidationError(_(
                "You must select an existing Customer (partner_id) to confirm a workshop appointment."
            ))

        vals = super(BusinessAppointmentCore, self)._prepare_vals_for_real_appointment_prereserv()
        vals.update({
            'vehicle_id': self.vehicle_id.id if self.vehicle_id else False,
            'current_odometer': self.current_odometer,
            'expected_delivery_date_time': self.expected_delivery_date_time,
            'is_authorized_service_contact': self.is_authorized_service_contact,
            'booking_request_key': self.booking_request_key,
            'contact_name': self.contact_name or (self.partner_id.name if self.partner_id else False),
            'phone': self.phone or (self.partner_id.phone if self.partner_id else False),
            'mobile': self.mobile or (self.partner_id.mobile if self.partner_id else False),
            'email': self.email or (self.partner_id.email if self.partner_id else False),
        })
        if self.vehicle_id:
            vals['registration_plate'] = self.vehicle_id.license_plate or self.registration_plate
            vals['vehicle_model'] = (
                self.vehicle_id.model_id.name if self.vehicle_id.model_id else self.vehicle_model
            )
        return vals

    def action_start_prereserv(self):
        already_confirmed = self.filtered(
            lambda r: r.state == 'processed'
            and self.env['business.appointment'].sudo().search_count([('prereservation_id', '=', r.id)]) > 0
        )
        to_start = self - already_confirmed
        if to_start:
            super(BusinessAppointmentCore, to_start).action_start_prereserv()
        return self

    def _confirm_prereserv(self, existing_appointment_ids=False):
        for core_id in sorted(self.ids):
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                (LOCK_NS_PRERESERVATION, core_id),
            )
        if not existing_appointment_ids:
            existing = self.env['business.appointment'].search([('prereservation_id', 'in', self.ids)])
            if existing and len(existing) == len(self):
                for app in existing:
                    if app._is_workshop_appointment() and not app.fleet_repair_id:
                        app._create_linked_fleet_repair()
                return existing
        return super(BusinessAppointmentCore, self)._confirm_prereserv(
            existing_appointment_ids=existing_appointment_ids
        )


class BusinessAppointment(models.Model):
    _inherit = 'business.appointment'

    fleet_repair_ids = fields.One2many(
        'fleet.repair',
        'appointment_id',
        string='Linked Service Repair Orders (SR)',
        readonly=True,
    )
    fleet_repair_id = fields.Many2one(
        'fleet.repair',
        string='Service Repair Order (SR)',
        compute='_compute_fleet_repair_id',
        compute_sudo=True,
        store=True,
        readonly=True,
        copy=False,
        help='Read-only reference computed from the unique link on fleet.repair.'
    )
    fleet_repair_count = fields.Integer(
        string='SR Count',
        compute='_compute_fleet_repair_id',
        compute_sudo=True,
        store=True,
    )
    fleet_repair_sequence = fields.Char(
        string='SR Reference',
        related='fleet_repair_id.sequence',
        readonly=True,
    )
    sr_reception_state = fields.Selection(
        related='fleet_repair_id.reception_state',
        string='SR Reception Status',
        readonly=True,
    )
    is_workshop_appointment = fields.Boolean(
        string='Is Workshop Appointment',
        compute='_compute_is_workshop_appointment',
        store=True,
        index=True,
    )

    @api.depends(
        'service_id',
        'service_id.is_workshop_service',
        'service_id.repair_service_type_id',
        'resource_id',
        'resource_id.resource_type_id.is_workshop_resource_type',
        'vehicle_id',
        'fleet_repair_id',
    )
    def _compute_is_workshop_appointment(self):
        for app in self:
            app.is_workshop_appointment = app._is_workshop_appointment()

    def _is_workshop_appointment(self):
        self.ensure_one()
        return bool(
            (self.service_id and (self.service_id.is_workshop_service or self.service_id.repair_service_type_id))
            or (self.resource_id and self.resource_id.resource_type_id.is_workshop_resource_type)
            or self.vehicle_id
            or self.fleet_repair_id
        )

    @api.depends('fleet_repair_ids', 'fleet_repair_ids.appointment_id')
    def _compute_fleet_repair_id(self):
        for app in self:
            repair = app.with_context(active_test=False).sudo().fleet_repair_ids[:1]
            if not repair and app.id:
                repair = self.env['fleet.repair'].with_context(active_test=False).sudo().search([
                    ('appointment_id', '=', app.id)
                ], limit=1)
            app.fleet_repair_id = repair.id if repair else False
            app.fleet_repair_count = 1 if repair else 0

    @api.onchange('vehicle_id')
    def _onchange_vehicle_id(self):
        for app in self:
            if app.vehicle_id:
                app.registration_plate = app.vehicle_id.license_plate or False
                app.vehicle_model = app.vehicle_id.model_id.name if app.vehicle_id.model_id else False
                owner = getattr(app.vehicle_id, 'customer_id', False) or app.vehicle_id.driver_id
                if not app.partner_id and owner:
                    app.partner_id = owner
                if not app.current_odometer and app.vehicle_id.odometer and app.vehicle_id.odometer > 0:
                    app.current_odometer = app.vehicle_id.odometer

    @api.onchange('datetime_start', 'datetime_end', 'service_id')
    def _onchange_schedule_expected_delivery(self):
        for app in self:
            if app.datetime_end and not app.expected_delivery_date_time:
                lead_hours = getattr(app.service_id, 'default_expected_delivery_lead_hours', 0.0) or 0.0
                app.expected_delivery_date_time = app.datetime_end + relativedelta(hours=lead_hours)

    @api.model
    def _acquire_locks_for_vals(self, vals):
        locks = []
        req_key = vals.get('booking_request_key')
        if req_key:
            locks.append((LOCK_NS_REQUEST_KEY, _stable_int32_hash(str(req_key))))
        prereserv_id = vals.get('prereservation_id')
        if prereserv_id:
            locks.append((LOCK_NS_PRERESERVATION, int(prereserv_id)))

        resource_ids = set()
        if vals.get('resource_id'):
            res_rec = self.env['business.resource'].sudo().browse(vals['resource_id'])
            if res_rec.exists():
                resource_ids.add((res_rec.info_resource_id or res_rec).id)
        extra_cmds = vals.get('extra_resource_ids') or []
        for cmd in extra_cmds:
            if isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[0] == 6:
                for rid in (cmd[2] or []):
                    er = self.env['business.resource'].sudo().browse(rid)
                    if er.exists():
                        resource_ids.add((er.info_resource_id or er).id)
            elif isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[0] == 4:
                er = self.env['business.resource'].sudo().browse(cmd[1])
                if er.exists():
                    resource_ids.add((er.info_resource_id or er).id)

        for rid in sorted(resource_ids):
            locks.append((LOCK_NS_RESOURCE, int(rid)))

        vehicle_id = vals.get('vehicle_id')
        if vehicle_id:
            locks.append((LOCK_NS_VEHICLE, int(vehicle_id)))

        for ns, key_id in sorted(set(locks)):
            self.env.cr.execute("SELECT pg_advisory_xact_lock(%s, %s)", (ns, key_id))

    def _acquire_record_locks(self):
        locks = set()
        for app in self:
            if app.id:
                locks.add((LOCK_NS_APPOINTMENT, int(app.id)))
            if app.booking_request_key:
                locks.add((LOCK_NS_REQUEST_KEY, _stable_int32_hash(str(app.booking_request_key))))
            if app.prereservation_id:
                locks.add((LOCK_NS_PRERESERVATION, int(app.prereservation_id.id)))
            for res in (app.resource_id | app.extra_resource_ids):
                eff_res = res.info_resource_id or res
                if eff_res.id:
                    locks.add((LOCK_NS_RESOURCE, int(eff_res.id)))
            if app.vehicle_id:
                locks.add((LOCK_NS_VEHICLE, int(app.vehicle_id.id)))

        for ns, key_id in sorted(locks):
            self.env.cr.execute("SELECT pg_advisory_xact_lock(%s, %s)", (ns, key_id))

    @api.model
    def _find_idempotent_existing_appointment(self, vals):
        prereserv_id = vals.get('prereservation_id')
        if prereserv_id:
            existing = self.search([('prereservation_id', '=', prereserv_id)], limit=1)
            if existing:
                return existing

        req_key = vals.get('booking_request_key')
        if req_key:
            domain = [
                ('booking_request_key', '=', req_key),
                ('state', '=', 'reserved'),
            ]
            if vals.get('partner_id'):
                domain.append(('partner_id', '=', vals['partner_id']))
            if vals.get('service_id'):
                domain.append(('service_id', '=', vals['service_id']))
            if vals.get('datetime_start'):
                domain.append(('datetime_start', '=', vals['datetime_start']))
            if vals.get('datetime_end'):
                domain.append(('datetime_end', '=', vals['datetime_end']))
            if vals.get('vehicle_id'):
                domain.append(('vehicle_id', '=', vals['vehicle_id']))
            existing = self.search(domain, limit=1)
            if existing:
                return existing
        return self.env['business.appointment']

    def _resolve_workshop_employee(self):
        self.ensure_one()
        if self.resource_id:
            emp = self.resource_id._get_workshop_employee()
            if emp:
                return emp
        for extra_res in self.extra_resource_ids:
            emp = extra_res._get_workshop_employee()
            if emp:
                return emp
        return self.env['hr.employee']

    def _check_effective_resource_and_vehicle_overlaps(self):
        for app in self:
            if app.state != 'reserved' or not app.datetime_start or not app.datetime_end:
                continue

            if app.datetime_end <= app.datetime_start:
                raise ValidationError(_(
                    "Invalid schedule: End datetime (%s) must be after start datetime (%s)."
                ) % (_format_aruba_dt(app.datetime_end), _format_aruba_dt(app.datetime_start)))

            effective_resources = (app.resource_id | app.extra_resource_ids).mapped('info_resource_id')
            if effective_resources:
                res_subdomains = []
                for eff_res in effective_resources:
                    res_subdomains = OR([
                        res_subdomains,
                        [('info_resource_id', '=', eff_res.id)],
                        [('extra_resource_ids.info_resource_id', '=', eff_res.id)],
                        [('extra_resource_ids', 'in', [eff_res.id] + eff_res.domain_resource_ids.ids)],
                    ])
                overlap_domain = AND([
                    res_subdomains,
                    [
                        ('id', '!=', app.id),
                        ('state', '=', 'reserved'),
                        ('datetime_start', '<', app.datetime_end),
                        ('datetime_end', '>', app.datetime_start),
                    ],
                ])
                conflict_app = self.sudo().search(overlap_domain, limit=1)
                if conflict_app:
                    raise ValidationError(_(
                        "Resource scheduling conflict: Resource '%(res)s' is already booked on appointment '%(app)s' from %(start)s to %(end)s."
                    ) % {
                        'res': ', '.join(effective_resources.mapped('name')),
                        'app': conflict_app.name or conflict_app.id,
                        'start': _format_aruba_dt(conflict_app.datetime_start),
                        'end': _format_aruba_dt(conflict_app.datetime_end),
                    })

            if app.vehicle_id:
                vehicle_domain = [
                    ('id', '!=', app.id),
                    ('vehicle_id', '=', app.vehicle_id.id),
                    ('state', '=', 'reserved'),
                    ('datetime_start', '<', app.datetime_end),
                    ('datetime_end', '>', app.datetime_start),
                ]
                conflict_veh_app = self.sudo().search(vehicle_domain, limit=1)
                if conflict_veh_app:
                    raise ValidationError(_(
                        "Vehicle scheduling conflict: Vehicle '%(veh)s' already has an active appointment ('%(app)s') during %(start)s - %(end)s."
                    ) % {
                        'veh': app.vehicle_id.license_plate or app.vehicle_id.name,
                        'app': conflict_veh_app.name or conflict_veh_app.id,
                        'start': _format_aruba_dt(conflict_veh_app.datetime_start),
                        'end': _format_aruba_dt(conflict_veh_app.datetime_end),
                    })

    def _check_calendar_leave_and_company_rules(self):
        for app in self:
            if app.state != 'reserved' or not app.datetime_start or not app.datetime_end:
                continue

            target_company = app.company_id or app.resource_id.company_id or app.service_id.company_id
            if app.service_id.company_id and target_company and app.service_id.company_id != target_company:
                raise ValidationError(_(
                    "Company conflict: Service '%s' belongs to company '%s', but the resource belongs to '%s'."
                ) % (app.service_id.name, app.service_id.company_id.name, target_company.name))

            for extra_res in app.extra_resource_ids:
                if extra_res.company_id and target_company and extra_res.company_id != target_company:
                    raise ValidationError(_(
                        "Company conflict: Extra resource '%s' belongs to another company ('%s')."
                    ) % (extra_res.name, extra_res.company_id.name))

            if app.service_id and app.service_id.extra_resource_ids:
                chosen_extra_prime_ids = set(app.extra_resource_ids.mapped('info_resource_id').ids)
                main_prime_id = app.resource_id.info_resource_id.id if app.resource_id else False
                for extra_line in app.service_id.extra_resource_ids:
                    allowed_extra_ids = set(extra_line._get_all_extra_resources().ids) - {main_prime_id}
                    if allowed_extra_ids and not (chosen_extra_prime_ids & allowed_extra_ids):
                        raise ValidationError(_(
                            "Service '%(srv)s' requires an additional resource for '%(line)s' that was not assigned."
                        ) % {
                            'srv': app.service_id.name,
                            'line': extra_line.name or ', '.join(extra_line._get_all_extra_resources().mapped('name')),
                        })

            start_utc = UTC.localize(fields.Datetime.to_datetime(app.datetime_start))
            end_utc = UTC.localize(fields.Datetime.to_datetime(app.datetime_end))
            required_seconds = (end_utc - start_utc).total_seconds()
            extra_srv_cal = app.service_id.extra_working_calendar_id if app.service_id else None

            for res in (app.resource_id | app.extra_resource_ids):
                info_res = res.info_resource_id or res
                cal = info_res.resource_calendar_id
                if not cal:
                    continue
                work_intervals = cal._ba_work_intervals(
                    start_dt=start_utc,
                    end_dt=end_utc,
                    resource=info_res.resource_id,
                    alias_resource_calendar_id=res.alias_resource_calendar_id,
                    extra_service_calendar_id=extra_srv_cal,
                )
                covered_seconds = sum(( iv[1] - iv[0] ).total_seconds() for iv in work_intervals)
                if covered_seconds + 1.0 < required_seconds:
                    raise ValidationError(_(
                        "Resource '%(res)s' is not available in their working calendar or has a leave registered during %(start)s - %(end)s."
                    ) % {
                        'res': res.name,
                        'start': _format_aruba_dt(app.datetime_start),
                        'end': _format_aruba_dt(app.datetime_end),
                    })

    def _validate_workshop_intake_contract(self):
        for app in self:
            if not app._is_workshop_appointment() or app.state in ('cancel', 'missed'):
                continue

            icp_sudo = self.env['ir.config_parameter'].sudo()
            if str(icp_sudo.get_param('alltocar_appointment_repair.disable_sr_integration', 'False')).lower() in ('1', 'true', 'yes'):
                raise UserError(_(
                    "Automatic Workshop Appointment to SR integration is temporarily disabled for maintenance/rollback. Please use the authorized manual reception procedure."
                ))

            if not app.service_id or not app.service_id.repair_service_type_id:
                raise UserError(_(
                    "Configuration Error: Service '%s' does not have a 'Mapped SR Service Type' (service.type) configured. Please configure the mapping before confirming the workshop appointment."
                ) % (app.service_id.name if app.service_id else _('No Service')))

            if not app.partner_id:
                raise ValidationError(_(
                    "You must select an existing Customer to confirm the workshop appointment."
                ))

            reachable_phone = (
                app.phone
                or app.mobile
                or getattr(app, 'mobile_phone', False)
                or app.partner_id.phone
                or app.partner_id.mobile
            )
            if not reachable_phone:
                raise ValidationError(_(
                    "You must provide at least one reachable phone or mobile number for customer '%s'."
                ) % app.partner_id.name)

            if getattr(app, 'send_confirmation_email', False) and not (app.email or app.partner_id.email):
                raise ValidationError(_(
                    "Confirmation email was requested, but customer '%s' does not have an email address."
                ) % app.partner_id.name)

            if not app.vehicle_id:
                raise ValidationError(_(
                    "You must select a Fleet Vehicle (vehicle_id) to confirm the workshop appointment."
                ))

            target_company = app.company_id or app.resource_id.company_id or self.env.company
            if app.vehicle_id.company_id and target_company and app.vehicle_id.company_id != target_company:
                raise ValidationError(_(
                    "Vehicle '%s' belongs to a different company than the appointment."
                ) % (app.vehicle_id.license_plate or app.vehicle_id.name))

            app._validate_vehicle_customer_authorization()

            if not app.current_odometer or app.current_odometer <= 0:
                raise ValidationError(_(
                    "You must enter a valid Reported Odometer (greater than 0) for vehicle '%s'."
                ) % (app.vehicle_id.license_plate or app.vehicle_id.name))

            if (
                app.vehicle_id.odometer
                and app.current_odometer < app.vehicle_id.odometer
                and not self.env.context.get('allow_lower_odometer')
            ):
                raise ValidationError(_(
                    "The reported odometer (%(rep)s) cannot be lower than the last recorded vehicle odometer (%(last)s)."
                ) % {
                    'rep': app.current_odometer,
                    'last': app.vehicle_id.odometer,
                })

            short_reason = (getattr(app, 'short_description', '') or '').strip()
            long_reason = (app.description or '').strip()
            if not short_reason and not long_reason:
                raise ValidationError(_(
                    "You must enter the visit reason or customer-reported symptoms (Short Description or Notes)."
                ))

            employee = app._resolve_workshop_employee()
            if not employee:
                raise UserError(_(
                    "Configuration Error: Resource '%s' does not have a workshop Employee (hr.employee) assigned. Please link the employee in resource configuration before confirming the appointment."
                ) % (app.resource_id.name if app.resource_id else ''))

            if employee.company_id and target_company and employee.company_id != target_company:
                raise ValidationError(_(
                    "Employee '%s' assigned to the resource belongs to another company ('%s')."
                ) % (employee.name, employee.company_id.name))

            if app.expected_delivery_date_time and app.expected_delivery_date_time < app.datetime_start:
                raise ValidationError(_(
                    "Estimated delivery date/time (%s) cannot be earlier than appointment start (%s)."
                ) % (
                    _format_aruba_dt(app.expected_delivery_date_time),
                    _format_aruba_dt(app.datetime_start),
                ))

    def _validate_vehicle_customer_authorization(self):
        self.ensure_one()
        vehicle = self.vehicle_id.sudo()
        partner = self.partner_id.sudo()
        if not vehicle or not partner:
            return

        is_external_caller = bool(
            self.env.user._is_public()
            or self.env.user.has_group('base.group_portal')
            or self.website_created
        )
        if self.env.user._is_public():
            raise AccessError(_(
                "Public unauthenticated users are not permitted to link internal fleet vehicles."
            ))

        if is_external_caller and self.env.user.has_group('base.group_portal'):
            caller_comm = self.env.user.partner_id.commercial_partner_id
            if partner.commercial_partner_id != caller_comm:
                raise AccessError(_(
                    "You do not have permission to book appointments on behalf of another customer."
                ))

        authorized_partners = self.env['res.partner'].sudo()
        if getattr(vehicle, 'customer_id', False):
            authorized_partners |= vehicle.customer_id
        if vehicle.driver_id:
            authorized_partners |= vehicle.driver_id
        if getattr(vehicle, 'partner_ids', False):
            authorized_partners |= vehicle.partner_ids

        if authorized_partners:
            authorized_commercial_ids = set(authorized_partners.mapped('commercial_partner_id').ids)
            is_owner_or_related = partner.commercial_partner_id.id in authorized_commercial_ids
            if not is_owner_or_related:
                if is_external_caller:
                    raise AccessError(_(
                        "Access denied: The selected vehicle is not registered under your customer account."
                    ))
                if not self.is_authorized_service_contact:
                    raise ValidationError(_(
                        "Vehicle '%(veh)s' belongs to '%(owner)s', which does not match the selected customer '%(client)s'. Please select the correct vehicle or check 'Authorized Service Contact' if another authorized person brings the vehicle."
                    ) % {
                        'veh': vehicle.license_plate or vehicle.name,
                        'owner': ', '.join(authorized_partners.mapped('name')),
                        'client': partner.name,
                    })

    @api.constrains(
        'vehicle_id',
        'resource_id',
        'extra_resource_ids',
        'datetime_start',
        'datetime_end',
        'state',
        'service_id',
        'partner_id',
        'current_odometer',
    )
    def _check_workshop_appointment_constraints(self):
        self._acquire_record_locks()
        self._check_effective_resource_and_vehicle_overlaps()
        self._check_calendar_leave_and_company_rules()
        self._validate_workshop_intake_contract()

    def _prepare_workshop_sr_reasons(self):
        self.ensure_one()
        short_desc = (getattr(self, 'short_description', '') or '').strip()
        long_desc = (self.description or '').strip()
        subject = short_desc or (long_desc.splitlines()[0] if long_desc else '') or (
            _('Workshop Appointment: %s') % (self.service_id.name or self.name or '')
        )
        if short_desc and long_desc and short_desc.lower() != long_desc.lower():
            full_symptoms = f"{short_desc}\n{long_desc}"
        else:
            full_symptoms = long_desc or short_desc
        return subject, full_symptoms

    def _prepare_fleet_repair_line_vals(self):
        self.ensure_one()
        _, full_symptoms = self._prepare_workshop_sr_reasons()
        return {
            'fleet_id': self.vehicle_id.id,
            'license_plate': self.vehicle_id.license_plate or self.registration_plate or False,
            'vin_sn': self.vehicle_id.vin_sn or False,
            'model_id': self.vehicle_id.model_id.id if self.vehicle_id.model_id else False,
            'fuel_type': self.vehicle_id.fuel_type or False,
            'service_type': self.service_id.repair_service_type_id.id,
            'service_detail': full_symptoms or False,
        }

    def _prepare_fleet_repair_vals(self):
        self.ensure_one()
        employee = self._resolve_workshop_employee()
        subject, full_symptoms = self._prepare_workshop_sr_reasons()
        service_type = self.service_id.repair_service_type_id
        target_company = self.company_id or self.resource_id.company_id or self.env.company

        lead_hours = getattr(self.service_id, 'default_expected_delivery_lead_hours', 0.0) or 0.0
        expected_delivery = self.expected_delivery_date_time or (
            (self.datetime_end + relativedelta(hours=lead_hours)) if self.datetime_end else self.datetime_start
        )

        contact_name = self.contact_name or self.partner_id.name
        phone_val = self.phone or self.partner_id.phone or self.mobile or getattr(self, 'mobile_phone', False) or self.partner_id.mobile or False
        mobile_val = self.mobile or getattr(self, 'mobile_phone', False) or self.partner_id.mobile or self.phone or self.partner_id.phone or False
        email_val = self.email or self.partner_id.email or False

        responsible_user = (
            self.user_id
            or self.resource_id.info_resource_id.user_id
            or employee.user_id
            or self.env.user
        )

        aruba_tz = timezone(ARUBA_TZ_NAME)
        start_aruba_date = (
            UTC.localize(fields.Datetime.to_datetime(self.datetime_start)).astimezone(aruba_tz).date()
            if self.datetime_start
            else fields.Date.context_today(self)
        )

        return {
            'name': subject,
            'appointment_id': self.id,
            'is_appointment_origin': True,
            'reception_state': 'booked',
            'state': 'draft',
            'client_id': self.partner_id.id,
            'contact_name': contact_name,
            'client_phone': phone_val,
            'phone': phone_val,
            'client_mobile': mobile_val,
            'client_email': email_val,
            'vehicle_id': self.vehicle_id.id,
            'fleet_id': self.vehicle_id.id,
            'license_plate': self.vehicle_id.license_plate or self.registration_plate or False,
            'vin_sn': self.vehicle_id.vin_sn or False,
            'model_id': self.vehicle_id.model_id.id if self.vehicle_id.model_id else False,
            'fuel_type': self.vehicle_id.fuel_type or False,
            'current_odometer': self.current_odometer,
            'reported_odometer': self.current_odometer,
            'previous_odometer': self.vehicle_id.odometer or 0.0,
            'is_odometer_verified': False,
            'service_type_id': service_type.id,
            'service_type': service_type.id,
            'conditions_text': service_type.conditions_text or False,
            'assigned_to_id': employee.id,
            'receiving_employee_id': False,
            'user_id': responsible_user.id if responsible_user else False,
            'company_id': target_company.id,
            'receipt_date_time': self.datetime_start,
            'receipt_date': start_aruba_date,
            'actual_receipt_datetime': False,
            'expected_delivery_date_time': expected_delivery,
            'planned_start_datetime': self.datetime_start,
            'planned_end_datetime': self.datetime_end,
            'description': full_symptoms or False,
            'service_detail': full_symptoms or False,
            'customer_symptoms': full_symptoms or False,
            'fleet_repair_line': [(0, 0, self._prepare_fleet_repair_line_vals())],
        }

    def _create_linked_fleet_repair(self):
        self.ensure_one()
        if not self._is_workshop_appointment():
            return False

        self._acquire_record_locks()

        existing_sr = self.env['fleet.repair'].with_context(active_test=False).sudo().search([
            ('appointment_id', '=', self.id)
        ], limit=1)
        if existing_sr:
            self._compute_fleet_repair_id()
            return existing_sr

        self._validate_workshop_intake_contract()

        repair_vals = self._prepare_fleet_repair_vals()
        new_sr = self.env['fleet.repair'].sudo().with_company(repair_vals['company_id']).create(repair_vals)
        self._compute_fleet_repair_id()

        audit_msg = _(
            "Draft Service Repair Order (SR) %(sr)s automatically created from Appointment %(app)s. "
            "Reception status: Booked / Awaiting Reception | Schedule: %(start)s - %(end)s."
        ) % {
            'sr': new_sr.sequence or new_sr.name,
            'app': self.name or self.id,
            'start': _format_aruba_dt(self.datetime_start),
            'end': _format_aruba_dt(self.datetime_end),
        }
        self.message_post(body=audit_msg)
        new_sr.message_post(body=audit_msg)
        _logger.info(
            "Workshop SR created atomically: appointment_ref=%s sr_ref=%s",
            self.name or self.id,
            new_sr.sequence or new_sr.id,
        )
        return new_sr

    def _sync_existing_sr_from_appointment(self, reason_label=None):
        self.ensure_one()
        sr = self.fleet_repair_id.sudo()
        if not sr:
            return False

        if sr._is_progressed_sr():
            raise UserError(_(
                "Cannot modify workshop appointment '%(app)s' schedule or data because the linked Service Repair Order '%(sr)s' has already been physically received or has work in progress."
            ) % {
                'app': self.name or self.id,
                'sr': sr.sequence or sr.name,
            })

        self._validate_workshop_intake_contract()
        mapped_vals = self._prepare_fleet_repair_vals()
        for immutable_key in ('appointment_id', 'is_appointment_origin', 'state', 'reception_state', 'fleet_repair_line'):
            mapped_vals.pop(immutable_key, None)

        sr.with_context(allow_appointment_link_sync=True).write(mapped_vals)

        line_vals = self._prepare_fleet_repair_line_vals()
        if sr.fleet_repair_line:
            sr.fleet_repair_line[0].write(line_vals)
        else:
            line_vals['fleet_repair_id'] = sr.id
            self.env['fleet.repair.line'].sudo().create(line_vals)

        audit_msg = _(
            "Appointment %(app)s updated (%(reason)s). SR %(sr)s synced: "
            "New schedule %(start)s - %(end)s."
        ) % {
            'app': self.name or self.id,
            'reason': reason_label or _('Reschedule / Update'),
            'sr': sr.sequence or sr.name,
            'start': _format_aruba_dt(self.datetime_start),
            'end': _format_aruba_dt(self.datetime_end),
        }
        self.message_post(body=audit_msg)
        sr.message_post(body=audit_msg)
        _logger.info(
            "Workshop SR synced: appointment_ref=%s sr_ref=%s reason=%s",
            self.name or self.id,
            sr.sequence or sr.id,
            reason_label or 'update',
        )
        return sr

    @api.model_create_multi
    def create(self, vals_list):
        existing_records = self.env['business.appointment']
        to_create_vals = []

        for vals in vals_list:
            self._acquire_locks_for_vals(vals)
            existing = self._find_idempotent_existing_appointment(vals)
            if existing:
                if existing._is_workshop_appointment() and not existing.fleet_repair_id:
                    existing._create_linked_fleet_repair()
                existing_records |= existing
            else:
                if vals.get('vehicle_id') and (not vals.get('registration_plate') or not vals.get('vehicle_model')):
                    veh = self.env['fleet.vehicle'].sudo().browse(vals['vehicle_id'])
                    if veh.exists():
                        vals.setdefault('registration_plate', veh.license_plate)
                        if veh.model_id:
                            vals.setdefault('vehicle_model', veh.model_id.name)
                if not vals.get('expected_delivery_date_time') and vals.get('datetime_end'):
                    dt_end = fields.Datetime.to_datetime(vals['datetime_end'])
                    lead_hours = 0.0
                    if vals.get('service_id'):
                        srv = self.env['appointment.product'].sudo().browse(vals['service_id'])
                        if srv.exists():
                            lead_hours = getattr(srv, 'default_expected_delivery_lead_hours', 0.0) or 0.0
                    vals['expected_delivery_date_time'] = dt_end + relativedelta(hours=lead_hours)
                to_create_vals.append(vals)

        created_records = self.env['business.appointment']
        if to_create_vals:
            created_records = super(BusinessAppointment, self).create(to_create_vals)
            for app in created_records:
                if app.state == 'reserved' and app._is_workshop_appointment():
                    app._create_linked_fleet_repair()

        return existing_records | created_records

    def write(self, vals):
        if self.env.context.get('skip_sr_lifecycle_sync'):
            return super(BusinessAppointment, self).write(vals)

        self._acquire_record_locks()

        protected_schedule_fields = {
            'datetime_start', 'datetime_end', 'resource_id', 'extra_resource_ids',
            'vehicle_id', 'service_id', 'partner_id',
        }
        sync_trigger_fields = protected_schedule_fields | {
            'current_odometer', 'expected_delivery_date_time', 'description',
            'short_description', 'contact_name', 'phone', 'mobile', 'email',
        }

        snapshots = {}
        for app in self:
            sr = app.fleet_repair_id.sudo()
            snapshots[app.id] = {
                'state': app.state,
                'datetime_start': app.datetime_start,
                'datetime_end': app.datetime_end,
                'resource_id': app.resource_id.id,
            }
            if sr:
                new_state = vals.get('state')
                if new_state == 'cancel' and sr._is_progressed_sr():
                    raise UserError(_(
                        "Blocked operation: Cannot cancel appointment '%(app)s' because its linked Service Repair Order '%(sr)s' has already progressed operatively (physically received or in progress). Authorized workshop review required."
                    ) % {
                        'app': app.name or app.id,
                        'sr': sr.sequence or sr.name,
                    })
                if new_state == 'missed' and sr._is_progressed_sr():
                    raise UserError(_(
                        "Blocked operation: Cannot mark appointment '%(app)s' as 'Missed' because the Service Repair Order '%(sr)s' was already physically received or started."
                    ) % {
                        'app': app.name or app.id,
                        'sr': sr.sequence or sr.name,
                    })
                if (protected_schedule_fields & set(vals.keys())) and sr._is_progressed_sr():
                    raise UserError(_(
                        "Blocked operation: Cannot reschedule or change vehicle/resource/customer on appointment '%(app)s' after Service Repair Order '%(sr)s' has started work or physical reception."
                    ) % {
                        'app': app.name or app.id,
                        'sr': sr.sequence or sr.name,
                    })

        res = super(BusinessAppointment, self).write(vals)
        self._acquire_record_locks()

        for app in self:
            prev = snapshots.get(app.id, {})
            prev_state = prev.get('state')
            sr = app.fleet_repair_id.sudo()

            if app.state == 'reserved' and app._is_workshop_appointment():
                if not sr:
                    app._create_linked_fleet_repair()
                elif prev_state in ('cancel', 'missed'):
                    sr.with_context(allow_appointment_link_sync=True).write({
                        'state': 'draft',
                        'reception_state': 'booked',
                    })
                    app._sync_existing_sr_from_appointment(reason_label=_('Appointment Restored'))
                    _logger.info(
                        "Workshop appointment restored: appointment_ref=%s sr_ref=%s",
                        app.name or app.id,
                        sr.sequence or sr.id,
                    )
                elif sync_trigger_fields & set(vals.keys()):
                    app._sync_existing_sr_from_appointment(reason_label=_('Reschedule / Edit'))

            elif app.state == 'cancel' and sr and prev_state != 'cancel':
                sr.with_context(allow_appointment_link_sync=True).write({
                    'state': 'cancel',
                    'reception_state': 'cancelled',
                })
                audit_msg = _(
                    "Appointment %(app)s cancelled. Linked Service Repair Order %(sr)s marked as Cancelled."
                ) % {
                    'app': app.name or app.id,
                    'sr': sr.sequence or sr.name,
                }
                app.message_post(body=audit_msg)
                sr.message_post(body=audit_msg)
                _logger.info(
                    "Workshop appointment cancelled: appointment_ref=%s sr_ref=%s",
                    app.name or app.id,
                    sr.sequence or sr.id,
                )

            elif app.state == 'missed' and sr and prev_state != 'missed':
                sr.with_context(allow_appointment_link_sync=True).write({
                    'reception_state': 'no_show',
                })
                audit_msg = _(
                    "Appointment %(app)s marked as Missed. SR %(sr)s updated to 'No-Show' status."
                ) % {
                    'app': app.name or app.id,
                    'sr': sr.sequence or sr.name,
                }
                app.message_post(body=audit_msg)
                sr.message_post(body=audit_msg)
                _logger.info(
                    "Workshop appointment marked missed: appointment_ref=%s sr_ref=%s",
                    app.name or app.id,
                    sr.sequence or sr.id,
                )

        return res

    def unlink(self):
        for app in self:
            if app.fleet_repair_id and not self.env.context.get('force_unlink_appointment_sr'):
                raise UserError(_(
                    "Cannot delete appointment '%s' because it has linked Service Repair Order '%s'. Please use the Cancel action to preserve audit trail."
                ) % (app.name or app.id, app.fleet_repair_id.sequence or app.fleet_repair_id.name))
        return super(BusinessAppointment, self).unlink()

    def action_consider_sales_order(self):
        non_workshop = self.filtered(lambda app: not app._is_workshop_appointment())
        if non_workshop and hasattr(super(BusinessAppointment, non_workshop), 'action_consider_sales_order'):
            return super(BusinessAppointment, non_workshop).action_consider_sales_order()
        return False

    @api.model
    def action_return_appointments_view(self, appointment_ids=None):
        raw_ids = appointment_ids or []
        if isinstance(raw_ids, int):
            raw_ids = [raw_ids]

        res_action = super(BusinessAppointment, self).action_return_appointments_view(raw_ids)

        domain = ['|', ('id', 'in', raw_ids), ('prereservation_id', 'in', raw_ids)]
        appointments = self.search(domain)
        if isinstance(res_action, dict) and res_action.get('res_id'):
            appointments |= self.browse(res_action['res_id'])

        for app in appointments:
            if app.state == 'reserved' and app._is_workshop_appointment() and not app.fleet_repair_id:
                app._create_linked_fleet_repair()

        return res_action

    def action_open_fleet_repair(self):
        self.ensure_one()
        if not self.fleet_repair_id:
            return False
        return {
            'name': _('Service Repair Order (SR)'),
            'type': 'ir.actions.act_window',
            'res_model': 'fleet.repair',
            'res_id': self.fleet_repair_id.id,
            'view_mode': 'form',
            'target': 'current',
        }
