# -*- coding: utf-8 -*-
from datetime import datetime
from unittest.mock import patch
from pytz import timezone, UTC

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install', 'alltocar_appointment_repair')
class TestWorkshopAppointmentSRIntegration(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other_company = cls.env['res.company'].create({'name': 'Other Workshop Co'})

        cls.aruba_calendar = cls.env['resource.calendar'].create({
            'name': 'Aruba Workshop Hours (08:00-17:00 AST)',
            'company_id': cls.company.id,
            'tz': 'America/Aruba',
            'attendance_ids': [
                (0, 0, {'name': 'Monday', 'dayofweek': '0', 'hour_from': 8.0, 'hour_to': 17.0}),
                (0, 0, {'name': 'Tuesday', 'dayofweek': '1', 'hour_from': 8.0, 'hour_to': 17.0}),
                (0, 0, {'name': 'Wednesday', 'dayofweek': '2', 'hour_from': 8.0, 'hour_to': 17.0}),
                (0, 0, {'name': 'Thursday', 'dayofweek': '3', 'hour_from': 8.0, 'hour_to': 17.0}),
                (0, 0, {'name': 'Friday', 'dayofweek': '4', 'hour_from': 8.0, 'hour_to': 17.0}),
            ],
        })

        cls.mechanic_emp_1 = cls.env['hr.employee'].create({
            'name': 'Mechanic One',
            'company_id': cls.company.id,
            'resource_calendar_id': cls.aruba_calendar.id,
        })
        cls.mechanic_emp_2 = cls.env['hr.employee'].create({
            'name': 'Mechanic Two',
            'company_id': cls.company.id,
            'resource_calendar_id': cls.aruba_calendar.id,
        })

        cls.workshop_resource_type = cls.env['business.resource.type'].create({
            'name': 'Workshop Mechanics',
            'company_id': cls.company.id,
            'is_workshop_resource_type': True,
            'service_method': 'multiple',
        })
        cls.bay_resource_type = cls.env['business.resource.type'].create({
            'name': 'Workshop Bays',
            'company_id': cls.company.id,
            'is_workshop_resource_type': False,
            'service_method': 'multiple',
        })

        cls.sr_service_type = cls.env['service.type'].create({
            'name': 'Preventive Maintenance 10K',
            'repair_title': 'Maintenance 10K',
            'conditions_text': '<p>Standard reception conditions.</p>',
        })
        cls.service_product = cls.env['product.product'].create({
            'name': 'Maintenance Appointment Service',
            'type': 'service',
        })
        cls.workshop_service = cls.env['appointment.product'].create({
            'name': 'Appointment Maintenance 10K',
            'product_id': cls.service_product.id,
            'company_id': cls.company.id,
            'is_workshop_service': True,
            'repair_service_type_id': cls.sr_service_type.id,
            'default_expected_delivery_lead_hours': 2.0,
        })
        cls.workshop_resource_type.write({'service_ids': [(6, 0, [cls.workshop_service.id])]})

        cls.resource_mech_1 = cls.env['business.resource'].create({
            'name': 'Mechanic Resource 1',
            'resource_type_id': cls.workshop_resource_type.id,
            'resource_type': 'user',
            'employee_id': cls.mechanic_emp_1.id,
            'resource_calendar_id': cls.aruba_calendar.id,
            'company_id': cls.company.id,
            'service_ids': [(6, 0, [cls.workshop_service.id])],
        })
        cls.resource_mech_2 = cls.env['business.resource'].create({
            'name': 'Mechanic Resource 2',
            'resource_type_id': cls.workshop_resource_type.id,
            'resource_type': 'user',
            'employee_id': cls.mechanic_emp_2.id,
            'resource_calendar_id': cls.aruba_calendar.id,
            'company_id': cls.company.id,
            'service_ids': [(6, 0, [cls.workshop_service.id])],
        })
        cls.resource_unmapped = cls.env['business.resource'].create({
            'name': 'Resource Without Employee',
            'resource_type_id': cls.workshop_resource_type.id,
            'resource_type': 'user',
            'employee_id': False,
            'resource_calendar_id': cls.aruba_calendar.id,
            'company_id': cls.company.id,
            'service_ids': [(6, 0, [cls.workshop_service.id])],
        })
        cls.resource_bay_1 = cls.env['business.resource'].create({
            'name': 'Lift Bay 1',
            'resource_type_id': cls.bay_resource_type.id,
            'resource_type': 'material',
            'resource_calendar_id': cls.aruba_calendar.id,
            'company_id': cls.company.id,
        })

        cls.customer = cls.env['res.partner'].create({
            'name': 'Customer Aruba Test',
            'phone': '+2975550101',
            'mobile': '+2975550102',
            'email': 'customer@alltocar.aw',
        })
        cls.other_customer = cls.env['res.partner'].create({
            'name': 'Other Unrelated Customer',
            'phone': '+2975550999',
            'email': 'unrelated@alltocar.aw',
        })

        cls.brand = cls.env['fleet.vehicle.model.brand'].create({
            'name': 'ToyotaTest',
            'brand_code': 'TYT',
        })
        cls.vehicle_model = cls.env['fleet.vehicle.model'].create({
            'name': 'Hilux',
            'brand_id': cls.brand.id,
        })
        cls.seller = cls.env['fleet.vehicle.seller'].create({
            'name': 'AllToCar Dealer',
            'our_dealer': True,
        })
        cls.vehicle = cls.env['fleet.vehicle'].create({
            'model_id': cls.vehicle_model.id,
            'license_plate': 'A-12345',
            'vin_sn': '1HGCM82633A004352',
            'customer_id': cls.customer.id,
            'driver_id': cls.customer.id,
            'vehicle_seller_id': cls.seller.id,
            'odometer': 15000.0,
            'fuel_type': 'gasoline',
            'company_id': cls.company.id,
        })

        cls.slot_start_utc = datetime(2026, 11, 9, 13, 0, 0)
        cls.slot_end_utc = datetime(2026, 11, 9, 14, 0, 0)

    def _base_appointment_vals(self, **overrides):
        vals = {
            'partner_id': self.customer.id,
            'resource_id': self.resource_mech_1.id,
            'service_id': self.workshop_service.id,
            'vehicle_id': self.vehicle.id,
            'current_odometer': 15500.0,
            'datetime_start': self.slot_start_utc,
            'datetime_end': self.slot_end_utc,
            'short_description': 'Front brake noise',
            'description': 'Customer reports squeaking when braking at low speed.',
            'state': 'reserved',
        }
        vals.update(overrides)
        return vals

    def test_at01_complete_workshop_booking_and_at02_navigation(self):
        app = self.env['business.appointment'].create([self._base_appointment_vals()])
        self.assertTrue(app.fleet_repair_id, "Exactly one draft Service Repair order must be created.")
        self.assertEqual(app.fleet_repair_count, 1)

        sr = app.fleet_repair_id
        self.assertEqual(sr.appointment_id, app)
        self.assertTrue(sr.is_appointment_origin)
        self.assertEqual(sr.state, 'draft')
        self.assertEqual(sr.reception_state, 'booked')
        self.assertTrue(sr.is_pending_reception)
        self.assertFalse(sr.actual_receipt_datetime)
        self.assertEqual(sr.client_id, self.customer)
        self.assertEqual(sr.vehicle_id, self.vehicle)
        self.assertEqual(sr.fleet_id, self.vehicle)
        self.assertEqual(sr.license_plate, 'A-12345')
        self.assertEqual(sr.vin_sn, '1HGCM82633A004352')
        self.assertEqual(sr.service_type_id, self.sr_service_type)
        self.assertEqual(sr.assigned_to_id, self.mechanic_emp_1)
        self.assertEqual(sr.reported_odometer, 15500.0)
        self.assertEqual(sr.current_odometer, 15500.0)
        self.assertFalse(sr.is_odometer_verified)
        self.assertEqual(self.vehicle.odometer, 15000.0)
        self.assertEqual(sr.planned_start_datetime, self.slot_start_utc)
        self.assertEqual(sr.planned_end_datetime, self.slot_end_utc)
        self.assertEqual(len(sr.fleet_repair_line), 1, "The vehicle repair line must be created.")

        act_sr = app.action_open_fleet_repair()
        self.assertEqual(act_sr['res_id'], sr.id)
        act_app = sr.action_open_business_appointment()
        self.assertEqual(act_app['res_id'], app.id)

    def test_at02_missing_mandatory_intake(self):
        with self.assertRaises(ValidationError):
            self.env['business.appointment'].create([self._base_appointment_vals(vehicle_id=False)])

        with self.assertRaises(ValidationError):
            self.env['business.appointment'].create([self._base_appointment_vals(current_odometer=0.0)])

        with self.assertRaises(ValidationError):
            self.env['business.appointment'].create([
                self._base_appointment_vals(short_description='', description='')
            ])

    def test_at03_unmapped_service_and_employee_blocked(self):
        unmapped_service = self.env['appointment.product'].create({
            'name': 'Workshop Service Without Mapping',
            'product_id': self.service_product.id,
            'is_workshop_service': True,
            'repair_service_type_id': False,
        })
        with self.assertRaises(UserError):
            self.env['business.appointment'].create([
                self._base_appointment_vals(service_id=unmapped_service.id)
            ])

        with self.assertRaises(UserError):
            self.env['business.appointment'].create([
                self._base_appointment_vals(resource_id=self.resource_unmapped.id)
            ])

    def test_at04_at05_idempotency_and_duplicate_prevention(self):
        req_key = 'idemp-key-001'
        app1 = self.env['business.appointment'].create([
            self._base_appointment_vals(booking_request_key=req_key)
        ])
        app2 = self.env['business.appointment'].create([
            self._base_appointment_vals(booking_request_key=req_key)
        ])
        self.assertEqual(app1.id, app2.id, "Idempotent retry must return the existing appointment.")
        self.assertEqual(app1.fleet_repair_id.id, app2.fleet_repair_id.id)
        sr_count = self.env['fleet.repair'].search_count([('appointment_id', '=', app1.id)])
        self.assertEqual(sr_count, 1)

    def test_at06_at07_at08_overlaps_and_back_to_back(self):
        self.env['business.appointment'].create([self._base_appointment_vals()])

        with self.assertRaises((ValidationError, UserError)):
            self.env['business.appointment'].create([
                self._base_appointment_vals(
                    resource_id=self.resource_mech_2.id,
                    datetime_start=datetime(2026, 11, 9, 13, 30, 0),
                    datetime_end=datetime(2026, 11, 9, 14, 30, 0),
                )
            ])

        app_b2b = self.env['business.appointment'].create([
            self._base_appointment_vals(
                datetime_start=datetime(2026, 11, 9, 14, 0, 0),
                datetime_end=datetime(2026, 11, 9, 15, 0, 0),
            )
        ])
        self.assertTrue(app_b2b.fleet_repair_id)

    def test_at09_at10_calendar_hours_and_aruba_timezone(self):
        aruba_tz = timezone('America/Aruba')
        local_9am = aruba_tz.localize(datetime(2026, 11, 10, 9, 0, 0))
        utc_equiv = local_9am.astimezone(UTC).replace(tzinfo=None)
        self.assertEqual(utc_equiv, datetime(2026, 11, 10, 13, 0, 0))

        with self.assertRaises(ValidationError):
            self.env['business.appointment'].create([
                self._base_appointment_vals(
                    datetime_start=datetime(2026, 11, 10, 23, 0, 0),
                    datetime_end=datetime(2026, 11, 11, 0, 0, 0),
                )
            ])

    def test_at11_reschedule_and_resource_reassignment_preserves_sr(self):
        app = self.env['business.appointment'].create([self._base_appointment_vals()])
        sr_id = app.fleet_repair_id.id
        sr_seq = app.fleet_repair_id.sequence

        new_start = datetime(2026, 11, 10, 15, 0, 0)
        new_end = datetime(2026, 11, 10, 16, 0, 0)
        app.write({
            'datetime_start': new_start,
            'datetime_end': new_end,
            'resource_id': self.resource_mech_2.id,
        })
        self.assertEqual(app.fleet_repair_id.id, sr_id)
        self.assertEqual(app.fleet_repair_id.sequence, sr_seq)
        self.assertEqual(app.fleet_repair_id.planned_start_datetime, new_start)
        self.assertEqual(app.fleet_repair_id.planned_end_datetime, new_end)
        self.assertEqual(app.fleet_repair_id.assigned_to_id, self.mechanic_emp_2)

    def test_at12_at13_cancel_restore_missed_and_physical_reception(self):
        app = self.env['business.appointment'].create([self._base_appointment_vals()])
        sr = app.fleet_repair_id

        app.action_mark_missed()
        self.assertEqual(sr.reception_state, 'no_show')

        app.action_restore()
        self.assertEqual(sr.state, 'draft')
        self.assertEqual(sr.reception_state, 'booked')

        app.action_cancel()
        self.assertEqual(sr.state, 'cancel')
        self.assertEqual(sr.reception_state, 'cancelled')

        app.action_restore()
        with self.assertRaises(UserError):
            sr.action_create_fleet_diagnosis()

        sr.action_confirm_physical_reception(verified_odometer=15620.0)
        self.assertEqual(sr.reception_state, 'received')
        self.assertTrue(sr.is_odometer_verified)
        self.assertTrue(sr.actual_receipt_datetime)
        self.assertEqual(sr.current_odometer, 15620.0)
        self.assertEqual(self.vehicle.odometer, 15620.0)

        with self.assertRaises(UserError):
            app.action_cancel()

    def test_at14_at15_at16_at17_unrelated_appointments_rollback_and_permissions(self):
        hr_type = self.env['business.resource.type'].create({
            'name': 'HR Interviews',
            'company_id': self.company.id,
            'is_workshop_resource_type': False,
            'service_method': 'multiple',
        })
        hr_service = self.env['appointment.product'].create({
            'name': 'General Interview',
            'product_id': self.service_product.id,
            'is_workshop_service': False,
        })
        hr_resource = self.env['business.resource'].create({
            'name': 'HR Room',
            'resource_type_id': hr_type.id,
            'resource_type': 'material',
            'resource_calendar_id': self.aruba_calendar.id,
            'company_id': self.company.id,
            'service_ids': [(6, 0, [hr_service.id])],
        })
        hr_app = self.env['business.appointment'].create([{
            'partner_id': self.customer.id,
            'resource_id': hr_resource.id,
            'service_id': hr_service.id,
            'datetime_start': datetime(2026, 11, 11, 13, 0, 0),
            'datetime_end': datetime(2026, 11, 11, 14, 0, 0),
            'state': 'reserved',
        }])
        self.assertFalse(hr_app.fleet_repair_id, "Non-workshop appointments must not create a Service Repair.")

        with self.assertRaises(ValidationError):
            self.env['business.appointment'].create([
                self._base_appointment_vals(
                    partner_id=self.other_customer.id,
                    datetime_start=datetime(2026, 11, 11, 15, 0, 0),
                    datetime_end=datetime(2026, 11, 11, 16, 0, 0),
                )
            ])

        with self.env.cr.savepoint():
            with patch.object(type(self.env['fleet.repair']), 'create', side_effect=UserError('Simulated SR Failure')):
                with self.assertRaises(UserError):
                    self.env['business.appointment'].create([
                        self._base_appointment_vals(
                            datetime_start=datetime(2026, 11, 12, 13, 0, 0),
                            datetime_end=datetime(2026, 11, 12, 14, 0, 0),
                        )
                    ])
        orphan_count = self.env['business.appointment'].search_count([
            ('datetime_start', '=', datetime(2026, 11, 12, 13, 0, 0)),
        ])
        self.assertEqual(orphan_count, 0, "No confirmed appointment must remain if Service Repair creation fails.")
