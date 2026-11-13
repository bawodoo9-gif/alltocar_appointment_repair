# AllToCar Workshop Appointment to Draft SR Integration (`alltocar_appointment_repair`)

Transactional integration in Odoo 16 between the appointment booking engine (`business.appointment`) and the workshop repair module (`fleet.repair` - Service Repair / SR) for **AllToCar Aruba**.

---

## 1. Extension Points and Architecture

* **Canonical Booking Models:** `business.appointment` and temporary pre-reservation `business.appointment.core` (both inheriting from `appointment.contact.info`), along with the OWL wizard `choose.appointment.customer`.
* **Pre-Reservation to Appointment Transfer Hook:** `business.appointment.core._prepare_vals_for_real_appointment_prereserv()` and `appointment.contact.info._return_appointment_values()` (used on reschedule).
* **Interoperability with Existing Addons:**
  * `universal_appointment_customization`: Compatible with fields `registration_plate`, `vehicle_model`, `short_description`, `mobile_phone`, and `send_confirmation_email`.
  * `business_appointment_sale`: Overrides `action_consider_sales_order()` so workshop appointments do not generate duplicate sales orders upon booking (workshop quotes originate from the diagnostic/SR workflow), preserving standard sales order behavior for non-workshop appointments.
  * `car_repair_industry` & `car_repair_management`: Atomic creation of `fleet.repair` header (with sequence `fleet.repair`) and mandatory `fleet.repair.line` required by `action_create_fleet_diagnosis` and reports.
  * `product_vehicle_management`: Ownership validation (`customer_id`, `driver_id`, `partner_ids`) and vehicle odometer preservation (`fleet.vehicle.odometer`) until physical reception.

---

## 2. Pre-Configuration Checklist

1. **Workshop Resource Types (`Settings / Appointments > Resource Types`):**
   * Enable **Workshop Resource Type (SR)** (`is_workshop_resource_type = True`) on mechanic/workshop resource types.
2. **Resource Employee Mapping (`Appointments > Resources`):**
   * Use filter **Human Resources Missing Employee (Pending SR Mapping)** to locate human resources without `employee_id`.
   * Assign the corresponding `hr.employee` to each mechanic resource. Bookings on unmapped resources are blocked with a descriptive configuration error.
3. **Workshop Service Mapping (`Appointments > Appointment Products`):**
   * Mark **Is Workshop Service (Requires SR)** (`is_workshop_service = True`).
   * Select **Mapped SR Service Type** (`repair_service_type_id` -> `service.type`).
   * Optionally configure **Additional Estimated Delivery Lead Hours (SR)** (`default_expected_delivery_lead_hours`).

---

## 3. Pre-Arrival vs. Physical Reception Lifecycle

* When a workshop appointment is confirmed (`state = 'reserved'`), exactly one `fleet.repair` is created atomically in `draft` state with:
  * `is_appointment_origin = True`
  * `reception_state = 'booked'` (**Booked / Awaiting Physical Reception**)
  * `planned_start_datetime` and `planned_end_datetime` (stored in UTC, validated and displayed in `America/Aruba`).
  * `reported_odometer` with customer-reported mileage (`is_odometer_verified = False`, without mutating `fleet.vehicle.odometer`).
* When the vehicle physically arrives at the workshop:
  * Reception opens the Service Repair order from the appointment or from the search filter **Booked / Awaiting Physical Reception**.
  * Verifies mileage in **Verified Odometer (Reception)** and clicks **Confirm Physical Reception** (`action_confirm_physical_reception`).
  * This records `actual_receipt_datetime`, sets `reception_state = 'received'`, sets `is_odometer_verified = True`, and updates the vehicle master odometer.

---

## 4. Technical and Concurrency Controls

* **Unique 1:1 Relationship:** SQL constraint `unique(appointment_id)` on `fleet.repair` with `ondelete='restrict'` and link-break protection in `write()` and `unlink()`.
* **Transactional Serialization:** `pg_advisory_xact_lock` acquired in deterministic order on `booking_request_key`, `prereservation_id`, effective resources (`info_resource_id` and `extra_resource_ids`), and `vehicle_id`.
* **Half-Open Interval Overlap Prevention:** Conflict check using `datetime_start < new_end AND datetime_end > new_start`, supporting back-to-back appointments (`end == next_start`).

---

## 5. Operations and Monitoring

* **Confirmed workshop appointments without SR (target: 0):**
  * UI Filter: `Workshop Appointments Missing SR (Alert)`
  * SQL Query:
    ```sql
    SELECT ba.id, ba.name, ba.datetime_start
    FROM business_appointment ba
    LEFT JOIN fleet_repair fr ON fr.appointment_id = ba.id
    WHERE ba.is_workshop_appointment IS TRUE
      AND ba.state = 'reserved'
      AND fr.id IS NULL;
    ```
* **SR Uniqueness Validation (target: 0 duplicates):**
  ```sql
  SELECT appointment_id, COUNT(*)
  FROM fleet_repair
  WHERE appointment_id IS NOT NULL
  GROUP BY appointment_id
  HAVING COUNT(*) > 1;
  ```

---

## 6. Deployment and Rollback

* **Module Upgrade:**
  ```bash
  odoo-bin -c odoo.conf -d <db_name> -u alltocar_appointment_repair --stop-after-init
  ```
* **Run Automated Tests:**
  ```bash
  odoo-bin -c odoo.conf -d <staging_db> --test-enable --test-tags alltocar_appointment_repair --stop-after-init
  ```
* **Contingency Circuit Breaker:**
  * To temporarily disable automated SR generation in case of operational issues without uninstalling the module, set system parameter:
    * Key: `alltocar_appointment_repair.disable_sr_integration`
    * Value: `True`
