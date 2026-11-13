# -*- coding: utf-8 -*-
{
    'name': 'AllToCar - Workshop Appointment to Draft SR Integration',
    'version': '16.0.1.1.0',
    'category': 'Services/Appointment',
    'summary': 'Atomic integration of Workshop Appointments to draft Service Repair (SR) with concurrency control, overlap protection, and physical reception workflow',
    'author': 'AllToCar / Cesar',
    'license': 'LGPL-3',
    'depends': [
        'business_appointment',
        'business_appointment_hr',
        'business_appointment_sale',
        'universal_appointment_customization',
        'car_repair_management',
        'car_repair_industry',
        'fleet',
    ],
    'data': [
        'security/alltocar_appointment_repair_security.xml',
        'security/ir.model.access.csv',
        'views/choose_customer_wizard_views.xml',
        'views/appointment_product_views.xml',
        'views/business_appointment_views.xml',
        'views/fleet_repair_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
