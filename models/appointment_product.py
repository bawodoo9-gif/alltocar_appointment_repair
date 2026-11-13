# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class BusinessResourceType(models.Model):
    _inherit = 'business.resource.type'

    is_workshop_resource_type = fields.Boolean(
        string='Workshop Resource Type (SR)',
        default=False,
        help='Indicates that this resource type corresponds to the mechanical workshop and its appointments must generate a draft Service Repair (SR).'
    )


class BusinessResource(models.Model):
    _inherit = 'business.resource'

    effective_employee_id = fields.Many2one(
        'hr.employee',
        string='Effective Employee (SR)',
        compute='_compute_effective_employee_id',
        compute_sudo=True,
        store=True,
        help='Employee linked directly to the resource or through its main resource (alias).'
    )

    @api.depends('employee_id', 'main_resource_id', 'main_resource_id.employee_id', 'info_resource_id.employee_id')
    def _compute_effective_employee_id(self):
        for resource in self:
            prime = resource.info_resource_id or resource.main_resource_id or resource
            resource.effective_employee_id = resource.employee_id or prime.employee_id

    def _get_workshop_employee(self):
        self.ensure_one()
        prime = self.info_resource_id or self.main_resource_id or self
        return self.employee_id or getattr(prime, 'employee_id', False)


class AppointmentProduct(models.Model):
    _inherit = 'appointment.product'

    is_workshop_service = fields.Boolean(
        string='Is Workshop Service (Requires SR)',
        default=False,
        help='If checked, any appointment confirmed with this service will require workshop intake data and will atomically create a draft Service Repair (SR).'
    )
    repair_service_type_id = fields.Many2one(
        'service.type',
        string='Mapped SR Service Type',
        help='Explicitly links this appointment service (appointment.product) to the workshop service type (service.type) for the Service Repair (SR).'
    )
    default_expected_delivery_lead_hours = fields.Float(
        string='Estimated Additional Lead Hours for Delivery (SR)',
        default=0.0,
        help='Delivery estimation policy: additional hours after the planned appointment end to calculate the default Expected Delivery Date/Time when not specified.'
    )

    @api.onchange('repair_service_type_id')
    def _onchange_repair_service_type_id(self):
        for rec in self:
            if rec.repair_service_type_id:
                rec.is_workshop_service = True
                if hasattr(rec, 'no_sales_order'):
                    rec.no_sales_order = True

    @api.onchange('is_workshop_service')
    def _onchange_is_workshop_service(self):
        for rec in self:
            if rec.is_workshop_service and hasattr(rec, 'no_sales_order'):
                rec.no_sales_order = True

    @api.constrains('default_expected_delivery_lead_hours')
    def _check_default_expected_delivery_lead_hours(self):
        for rec in self:
            if rec.default_expected_delivery_lead_hours < 0:
                raise ValidationError(_("Additional estimated delivery lead hours cannot be negative."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('repair_service_type_id') and 'is_workshop_service' not in vals:
                vals['is_workshop_service'] = True
            if vals.get('is_workshop_service') and 'no_sales_order' in self._fields and 'no_sales_order' not in vals:
                vals['no_sales_order'] = True
        return super(AppointmentProduct, self).create(vals_list)

    def write(self, vals):
        if vals.get('repair_service_type_id') and 'is_workshop_service' not in vals:
            vals = dict(vals, is_workshop_service=True)
        return super(AppointmentProduct, self).write(vals)
