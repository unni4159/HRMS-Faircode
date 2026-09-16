
import frappe
from frappe import _
from datetime import datetime, timedelta
from frappe.utils import get_datetime, now_datetime, getdate


def get_session_employee():
	"""
	Returns the Employee doc name linked to the current logged-in user.
	"""
	user = frappe.session.user
	if not user or user == "Guest":
		return None
	return (
		frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")
		or frappe.db.get_value("Employee", {"company_email": user, "status": "Active"}, "name")
		or frappe.db.get_value("Employee", {"personal_email": user, "status": "Active"}, "name")
	)


def validate_not_candidate():
	"""
	Strictly blocks Candidate accounts from calling Employee/Admin endpoints.
	"""
	user = frappe.session.user
	if user and user != "Guest":
		roles = set(frappe.get_roles(user))
		if "Candidate" in roles and not (roles & {"System Manager", "Administrator", "HR Manager"}):
			raise frappe.PermissionError(_("Access Denied: Candidate accounts cannot access Employee features."))


def validate_employee_access(employee=None):
	"""
	Ensures that non-admin users can ONLY access/modify their OWN employee records.
	Candidate and Guest users are strictly blocked.
	"""
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("Authentication required. Please log in."), frappe.AuthenticationError)

	roles = set(frappe.get_roles(user))
	is_admin = bool(roles & {"System Manager", "Administrator", "HR Manager", "HR User"}) or user == "Administrator"

	if "Candidate" in roles and not is_admin:
		raise frappe.PermissionError(_("Access Denied: Candidate accounts cannot access Employee features."))

	session_emp = get_session_employee()
	if not is_admin:
		if not session_emp:
			frappe.throw(_("No active employee profile linked to your user account."), frappe.PermissionError)
		if employee and employee != session_emp:
			frappe.throw(_("Permission Denied: You cannot access or modify another employee's records."), frappe.PermissionError)

	return employee or session_emp


def _sync_attendance_for_session(emp, shift_date, in_time, out_time, shift_name, working_hours, status="Present"):
	"""
	Creates or updates ERPNext Attendance anchored to the shift start date:
	- Ignores cancelled records (docstatus != 2)
	- Sets working hours, in_time, out_time, shift
	- Submits Attendance (docstatus = 1)
	"""
	att_name = frappe.db.get_value(
		"Attendance",
		{"employee": emp, "attendance_date": shift_date, "docstatus": ["!=", 2]},
		"name",
	)

	if att_name:
		att_doc = frappe.get_doc("Attendance", att_name)
		if att_doc.docstatus == 1:
			frappe.db.set_value(
				"Attendance",
				att_name,
				{
					"status": status,
					"working_hours": working_hours,
					"in_time": in_time,
					"out_time": out_time,
					"shift": shift_name,
				},
			)
		else:
			att_doc.status = status
			att_doc.working_hours = working_hours
			att_doc.in_time = in_time
			att_doc.out_time = out_time
			att_doc.shift = shift_name
			att_doc.flags.ignore_permissions = True
			att_doc.save()
			try:
				att_doc.submit()
			except Exception:
				pass
	else:
		att_doc = frappe.new_doc("Attendance")
		att_doc.employee = emp
		att_doc.attendance_date = shift_date
		att_doc.status = status
		att_doc.working_hours = working_hours
		att_doc.in_time = in_time
		att_doc.out_time = out_time
		att_doc.shift = shift_name
		att_doc.flags.ignore_permissions = True
		att_doc.insert()
		try:
			att_doc.submit()
		except Exception:
			pass


@frappe.whitelist()
def record_employee_checkin(employee=None, time=None, device_id="Employee Web Terminal"):
	"""
	Records a biometric / web terminal Check-In (IN) in ERPNext:
	- Enforces employee ownership
	- Verifies that no active IN session exists (prevents duplicate INs)
	- Idempotent within 60s (handles double-clicks / network retries)
	- Associates with current active Shift Assignment
	- Reconciles Attendance
	"""
	emp = validate_employee_access(employee)
	in_time = get_datetime(time) if time else now_datetime()

	# 1. Check if employee already has an open IN checkin
	latest_checkin = frappe.db.get_value(
		"Employee Checkin",
		{"employee": emp},
		["name", "time", "log_type", "shift", "device_id"],
		order_by="time desc, creation desc",
		as_dict=True,
	)

	if latest_checkin and latest_checkin.log_type == "IN":
		has_out = frappe.db.exists(
			"Employee Checkin",
			{
				"employee": emp,
				"log_type": "OUT",
				"time": [">=", latest_checkin.time],
				"name": ["!=", latest_checkin.name],
			},
		)
		if not has_out:
			time_diff = abs((in_time - get_datetime(latest_checkin.time)).total_seconds())
			if time_diff < 60:
				return frappe.get_doc("Employee Checkin", latest_checkin.name).as_dict()
			frappe.throw(
				_("You are already checked in (Checked in at {0}). Please check out before checking in again.").format(
					latest_checkin.time
				),
				frappe.ValidationError,
			)

	# 2. Determine assigned shift
	shift_name = None
	assignment = frappe.db.get_value(
		"Shift Assignment",
		{
			"employee": emp,
			"docstatus": 1,
			"start_date": ["<=", in_time.date()],
		},
		["shift_type", "start_date", "end_date"],
		as_dict=True,
		order_by="start_date desc",
	)
	if assignment:
		shift_name = assignment.shift_type
	if not shift_name:
		shift_name = "36 Hour Shift"

	# 3. Create single Employee Checkin record
	checkin = frappe.new_doc("Employee Checkin")
	checkin.employee = emp
	checkin.time = in_time
	checkin.log_type = "IN"
	checkin.shift = shift_name
	checkin.device_id = device_id or "Employee Web Terminal"
	checkin.flags.ignore_permissions = True
	checkin.insert()
	frappe.db.commit()

	return checkin.as_dict()


@frappe.whitelist()
def record_employee_checkout(employee=None, time=None, device_id="Employee Web Terminal"):
	"""
	Records a biometric / web terminal Check-Out (OUT) in ERPNext:
	- Enforces employee ownership
	- Verifies that an active IN session exists
	- Idempotent within 60s of an existing OUT
	- Creates exactly ONE OUT checkin
	- Automatically creates or updates ERPNext Attendance anchored to the shift start date
	"""
	emp = validate_employee_access(employee)
	out_time = get_datetime(time) if time else now_datetime()

	# 1. Find the latest IN checkin
	active_in = frappe.db.get_value(
		"Employee Checkin",
		{"employee": emp, "log_type": "IN"},
		["name", "time", "shift", "device_id"],
		order_by="time desc",
		as_dict=True,
	)

	if not active_in:
		frappe.throw(_("No check-in record found. Please check in first."), frappe.ValidationError)

	in_time = get_datetime(active_in.time)

	# 2. Check if active_in already has a corresponding OUT after it
	existing_out = frappe.db.get_value(
		"Employee Checkin",
		{"employee": emp, "log_type": "OUT", "time": [">", active_in.time]},
		["name", "time", "device_id"],
		order_by="time asc",
		as_dict=True,
	)

	if existing_out:
		frappe.throw(
			_("You have already checked out for your active session (Checked out at {0}). Please check in first.").format(
				existing_out.time
			),
			frappe.ValidationError,
		)

	in_time_sec = in_time.replace(microsecond=0)
	out_time_sec = out_time.replace(microsecond=0)
	if out_time_sec <= in_time_sec:
		out_time = in_time_sec + timedelta(seconds=1)
	else:
		out_time = out_time_sec

	shift_name = active_in.shift or "36 Hour Shift"

	# 3. Create single Employee Checkin (OUT)
	checkout = frappe.new_doc("Employee Checkin")
	checkout.employee = emp
	checkout.time = out_time
	checkout.log_type = "OUT"
	checkout.shift = shift_name
	checkout.device_id = device_id or "Employee Web Terminal"
	checkout.flags.ignore_permissions = True
	checkout.insert()

	# 4. Generate / update ERPNext Attendance
	shift_date = in_time.date()
	working_hours = round((out_time - in_time).total_seconds() / 3600.0, 2)
	_sync_attendance_for_session(emp, shift_date, in_time, out_time, shift_name, working_hours, "Present")

	frappe.db.commit()
	return checkout.as_dict()


@frappe.whitelist()
@frappe.whitelist(allow_guest=True)
def process_auto_checkout_and_attendance(employee=None, force_checkout=False):
	validate_not_candidate()
	now = now_datetime()
	target_employees = (
		[employee]
		if employee
		else [e.name for e in frappe.get_all("Employee", filters={"status": "Active"}, fields=["name"])]
	)

	results = []
	for emp in target_employees:
		# Find all IN checkins for this employee
		all_ins = frappe.db.get_all(
			"Employee Checkin",
			filters={"employee": emp, "log_type": "IN"},
			fields=["name", "time", "shift", "device_id"],
			order_by="time asc",
		)

		for in_rec in all_ins:
			in_time = get_datetime(in_rec.time)
			shift_date = in_time.date()

			# Find next IN checkin after this one
			next_in_time = frappe.db.get_value(
				"Employee Checkin",
				{
					"employee": emp,
					"log_type": "IN",
					"time": [">", in_rec.time],
				},
				"time",
				order_by="time asc",
			)

			# Look for any OUT checkin between this in_time and next_in_time
			out_filters = {"employee": emp, "log_type": "OUT"}
			if next_in_time:
				out_filters["time"] = ["between", [in_rec.time, next_in_time]]
			else:
				out_filters["time"] = [">=", in_rec.time]

			existing_outs = frappe.db.get_all(
				"Employee Checkin",
				filters=out_filters,
				fields=["name", "time", "device_id"],
				order_by="time asc",
			)
			existing_out = existing_outs[0] if existing_outs else None

			# Determine assigned shift & duration
			shift_name = in_rec.shift
			if not shift_name:
				assignment = frappe.db.get_value(
					"Shift Assignment",
					{
						"employee": emp,
						"docstatus": 1,
						"start_date": ["<=", shift_date],
					},
					["shift_type", "start_date", "end_date"],
					as_dict=True,
					order_by="start_date desc",
				)
				if assignment:
					shift_name = assignment.shift_type

			duration_hours = 8.0
			is_36h_shift = False
			if shift_name:
				if frappe.db.exists("Shift Type", shift_name):
					st = frappe.get_doc("Shift Type", shift_name)
					configured_duration = getattr(st, "custom_shift_duration_hours", None)
					if configured_duration and float(configured_duration) > 0:
						duration_hours = float(configured_duration)
					elif "36" in str(shift_name) or "Thirty Six" in str(shift_name):
						duration_hours = 36.0
				elif "36" in str(shift_name):
					duration_hours = 36.0

			if duration_hours >= 36.0 or ("36" in str(shift_name) if shift_name else False):
				is_36h_shift = True

			expected_end_time = in_time + timedelta(hours=duration_hours) if is_36h_shift else None

			auto_checkout_created = False
			if is_36h_shift and not existing_out:
				if now >= expected_end_time or force_checkout:
					existing_exact = frappe.db.exists(
						"Employee Checkin",
						{
							"employee": emp,
							"log_type": "OUT",
							"time": expected_end_time,
						},
					)
					if not existing_exact:
						out_doc = frappe.new_doc("Employee Checkin")
						out_doc.employee = emp
						out_doc.time = expected_end_time
						out_doc.log_type = "OUT"
						out_doc.shift = shift_name or "36 Hour Shift"
						out_doc.device_id = "Auto 36H Checkout"
						out_doc.flags.ignore_permissions = True
						out_doc.insert()
						existing_out = {
							"name": out_doc.name,
							"time": out_doc.time,
							"device_id": "Auto 36H Checkout",
						}
						auto_checkout_created = True

			# Sync Attendance if OUT exists
			if existing_out:
				out_time = get_datetime(existing_out["time"])
				working_hours = round((out_time - in_time).total_seconds() / 3600.0, 2)
				_sync_attendance_for_session(
					emp,
					shift_date,
					in_time,
					out_time,
					shift_name or "36 Hour Shift",
					working_hours,
					"Present",
				)
				results.append({
					"employee": emp,
					"in_time": str(in_time),
					"expected_end_time": str(expected_end_time) if expected_end_time else None,
					"out_time": str(existing_out["time"]),
					"is_36h_shift": is_36h_shift,
					"auto_checkout_created": auto_checkout_created,
					"status": "Present",
					"working_hours": working_hours,
				})
			else:
				results.append({
					"employee": emp,
					"in_time": str(in_time),
					"expected_end_time": str(expected_end_time) if expected_end_time else None,
					"out_time": None,
					"is_36h_shift": is_36h_shift,
					"auto_checkout_created": False,
					"status": "Pending",
					"working_hours": 0.0,
				})

	frappe.db.commit()
	return results


@frappe.whitelist()
def get_employee_shift_status(employee=None):
	"""
	Returns active shift status, last check-in, last check-out, expected end, and checkout source.
	Enforces employee access ownership.
	"""
	emp = validate_employee_access(employee)
	if not emp:
		return {}

	today = getdate()
	assignment = frappe.db.get_value(
		"Shift Assignment",
		{
			"employee": emp,
			"docstatus": 1,
			"start_date": ["<=", today],
		},
		["shift_type", "start_date", "end_date"],
		as_dict=True,
		order_by="start_date desc",
	)

	last_checkin = frappe.db.get_value(
		"Employee Checkin",
		{"employee": emp, "log_type": "IN"},
		["name", "time", "shift", "device_id"],
		as_dict=True,
		order_by="time desc, creation desc",
	)

	last_checkout = None
	if last_checkin:
		last_checkout = frappe.db.get_value(
			"Employee Checkin",
			{
				"employee": emp,
				"log_type": "OUT",
				"time": [">=", last_checkin.time],
				"name": ["!=", last_checkin.name],
			},
			["name", "time", "device_id"],
			as_dict=True,
			order_by="time desc, creation desc",
		)

	shift_name = assignment.shift_type if assignment else (last_checkin.shift if last_checkin else None)
	is_36h = False
	duration_hours = 8.0
	if shift_name:
		if frappe.db.exists("Shift Type", shift_name):
			st = frappe.get_doc("Shift Type", shift_name)
			duration_hours = float(getattr(st, "custom_shift_duration_hours", 0.0) or (36.0 if "36" in str(shift_name) else 8.0))
		elif "36" in str(shift_name):
			duration_hours = 36.0

	if duration_hours >= 36.0 or ("36" in str(shift_name) if shift_name else False):
		is_36h = True

	expected_shift_end = None
	if last_checkin and is_36h:
		expected_shift_end = get_datetime(last_checkin.time) + timedelta(hours=duration_hours)

	is_checked_in = bool(last_checkin and not last_checkout)
	is_auto_checkout = bool(last_checkout and last_checkout.device_id == "Auto 36H Checkout")

	return {
		"employee": emp,
		"shift": shift_name or "36 Hour Shift",
		"duration_hours": duration_hours,
		"is_36h_shift": is_36h,
		"is_checked_in": is_checked_in,
		"last_in_time": str(last_checkin.time) if last_checkin else None,
		"last_out_time": str(last_checkout.time) if last_checkout else None,
		"expected_shift_end": str(expected_shift_end) if expected_shift_end else None,
		"expected_checkout": str(expected_shift_end) if expected_shift_end else None,
		"is_auto_checkout": is_auto_checkout,
	}


@frappe.whitelist()
def get_employee_session_status(employee=None):
	res = get_employee_shift_status(employee=employee)
	res["status"] = "IN" if res.get("is_checked_in") else "OUT"
	return res


@frappe.whitelist()
def update_employee_profile(employee=None, data=None, **kwargs):
	"""
	Allows employees to update ONLY permitted profile fields:
	- personal_email
	- cell_number
	- gender
	- date_of_birth
	Restricted fields (employee_name, department, designation, company, status, salary, etc.)
	CANNOT be modified by employees.
	"""
	emp = validate_employee_access(employee)
	if isinstance(data, str):
		import json
		try:
			data = json.loads(data)
		except Exception:
			data = {}
	elif not data:
		data = kwargs

	doc = frappe.get_doc("Employee", emp)

	user = frappe.session.user
	roles = set(frappe.get_roles(user))
	is_admin = bool(roles & {"System Manager", "Administrator", "HR Manager"}) or user == "Administrator"

	restricted_fields = ["name", "employee_name", "department", "designation", "company", "status", "employment_type"]
	if not is_admin:
		for fld in restricted_fields:
			if fld in data and str(data[fld]).strip() != str(getattr(doc, fld, "")).strip():
				frappe.throw(_("Permission Denied: You cannot modify restricted field '{0}'.").format(fld), frappe.PermissionError)

	# Update permitted fields
	if "personal_email" in data:
		doc.personal_email = str(data["personal_email"]).strip()
	if "cell_number" in data:
		doc.cell_number = str(data["cell_number"]).strip()
	if "gender" in data and (is_admin or data.get("gender")):
		doc.gender = data["gender"]
	if "date_of_birth" in data and (is_admin or data.get("date_of_birth")):
		doc.date_of_birth = data["date_of_birth"]

	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist()
@frappe.whitelist(allow_guest=True)
def record_overtime(employee, date, ot_hours, current_salary=None, working_days=26, overtime_rate=1.75, attendance=None, shift=None, remarks=None):
	validate_not_candidate()
	"""
	Calculates and stores approved overtime:
	Formula: OT Hours × (Current Salary / Working Days / 8) × 1.75
	"""
	ot_hours = float(ot_hours)
	working_days = float(working_days) or 26.0
	overtime_rate = float(overtime_rate) or 1.75

	if not current_salary or float(current_salary) <= 0:
		ssa = frappe.db.get_value("Salary Structure Assignment", {"employee": employee, "docstatus": 1, "from_date": ["<=", date]}, "base", order_by="from_date desc")
		current_salary = float(ssa or 40000.0)
	else:
		current_salary = float(current_salary)

	hourly_rate = current_salary / working_days / 8.0
	overtime_amount = round(ot_hours * hourly_rate * overtime_rate, 2)

	existing = frappe.db.get_value("Employee Overtime Log", {"employee": employee, "date": date}, "name")
	if existing:
		doc = frappe.get_doc("Employee Overtime Log", existing)
		doc.ot_hours = ot_hours
		doc.current_salary = current_salary
		doc.working_days = working_days
		doc.overtime_rate = overtime_rate
		doc.overtime_amount = overtime_amount
		if hasattr(doc, "attendance") and attendance:
			doc.attendance = attendance
		if hasattr(doc, "shift") and shift:
			doc.shift = shift
		if hasattr(doc, "approval_status"):
			doc.approval_status = "Approved"
		if hasattr(doc, "remarks") and remarks:
			doc.remarks = remarks
		doc.flags.ignore_permissions = True
		doc.save()
	else:
		doc = frappe.new_doc("Employee Overtime Log")
		doc.employee = employee
		doc.date = date
		doc.ot_hours = ot_hours
		doc.current_salary = current_salary
		doc.working_days = working_days
		doc.overtime_rate = overtime_rate
		doc.overtime_amount = overtime_amount
		if hasattr(doc, "attendance") and attendance:
			doc.attendance = attendance
		if hasattr(doc, "shift") and shift:
			doc.shift = shift
		if hasattr(doc, "approval_status"):
			doc.approval_status = "Approved"
		if hasattr(doc, "remarks") and remarks:
			doc.remarks = remarks
		doc.flags.ignore_permissions = True
		doc.insert()

	ot_date = getdate(date)
	draft_slips = frappe.get_all(
		"Salary Slip",
		filters={
			"employee": employee,
			"docstatus": 0,
			"start_date": ["<=", ot_date],
			"end_date": [">=", ot_date],
		},
		fields=["name", "custom_overtime_hours"],
	)
	for slip in draft_slips:
		cur_hours = float(slip.custom_overtime_hours or 0.0)
		frappe.db.set_value("Salary Slip", slip.name, "custom_overtime_hours", cur_hours + ot_hours)

	frappe.db.commit()
	return doc.as_dict()


def validate_overtime_log(doc, method=None):
	"""
	Calculates overtime amount automatically on validate:
	OT Hours × (Current Salary / Working Days / 8) × 1.75
	"""
	if not doc.current_salary or float(doc.current_salary) <= 0:
		ssa = frappe.db.get_value(
			"Salary Structure Assignment",
			{"employee": doc.employee, "docstatus": 1, "from_date": ["<=", doc.date]},
			"base",
			order_by="from_date desc",
		)
		doc.current_salary = float(ssa or 40000.0)

	if not doc.working_days or float(doc.working_days) <= 0:
		doc.working_days = 26.0

	if not doc.overtime_rate or float(doc.overtime_rate) <= 0:
		doc.overtime_rate = 1.75

	hourly_rate = float(doc.current_salary) / float(doc.working_days) / 8.0
	doc.overtime_amount = round(float(doc.ot_hours) * hourly_rate * float(doc.overtime_rate), 2)


def sync_overtime_to_salary_slip(doc, method=None):
	"""
	Sums Employee Overtime Log hours for the employee and pay period into custom_overtime_hours.
	"""
	if not getattr(doc, "employee", None) or not getattr(doc, "start_date", None) or not getattr(doc, "end_date", None):
		return

	if not frappe.db.table_exists("Employee Overtime Log"):
		return

	total_ot = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(ot_hours), 0)
		FROM `tabEmployee Overtime Log`
		WHERE employee = %s AND date BETWEEN %s AND %s
		""",
		(doc.employee, doc.start_date, doc.end_date),
	)[0][0]

	if float(total_ot) > 0:
		doc.custom_overtime_hours = float(total_ot)


@frappe.whitelist()
@frappe.whitelist(allow_guest=True)
def screen_applicant(applicant, status, notes=None):
	"""
	Updates Job Applicant status and records screening notes.
	"""
	app = frappe.get_doc("Job Applicant", applicant)
	app.status = status
	if notes:
		frappe.get_doc({
			"doctype": "Comment",
			"comment_type": "Comment",
			"reference_doctype": "Job Applicant",
			"reference_name": applicant,
			"content": f"Screening Note: {notes}",
		}).insert(ignore_permissions=True)
	app.flags.ignore_permissions = True
	app.save()
	frappe.db.commit()

	try:
		from hrms.api.candidate_api import sync_job_applicant_to_candidate_app
		sync_job_applicant_to_candidate_app(app)
	except Exception:
		pass

	return app.as_dict()


@frappe.whitelist()
@frappe.whitelist(allow_guest=True)
def schedule_interview(applicant, interview_type, scheduled_on, from_time="10:00:00", to_time="11:00:00", interviewer=None):
	"""
	Schedules an interview round for applicant. If an existing pending interview exists for this round, updates it.
	"""
	existing = frappe.db.get_value("Interview", {"job_applicant": applicant, "interview_type": interview_type, "status": "Pending"}, "name")
	if existing:
		frappe.db.set_value("Interview", existing, {
			"scheduled_on": scheduled_on,
			"from_time": from_time,
			"to_time": to_time,
		})
		interview = frappe.get_doc("Interview", existing)
	else:
		interview = frappe.new_doc("Interview")
		interview.job_applicant = applicant
		interview.interview_type = interview_type
		interview.scheduled_on = scheduled_on
		interview.from_time = from_time
		interview.to_time = to_time
		interview.status = "Pending"
		interview.flags.ignore_permissions = True
		interview.insert()

	app = frappe.get_doc("Job Applicant", applicant)
	if "1" in interview_type or "Round 1" in interview_type:
		app.status = "Interview Round 1"
	elif "2" in interview_type or "Round 2" in interview_type:
		app.status = "Interview Round 2"
	elif "HR" in interview_type:
		app.status = "Final Selection"
	app.flags.ignore_permissions = True
	app.save()

	frappe.db.commit()

	try:
		from hrms.api.candidate_api import sync_interview_to_candidate_app, sync_job_applicant_to_candidate_app
		sync_interview_to_candidate_app(interview)
		sync_job_applicant_to_candidate_app(app)
	except Exception:
		pass

	return interview.as_dict()


@frappe.whitelist()
@frappe.whitelist(allow_guest=True)
def submit_interview_evaluation(interview_name, technical_score=0, communication_score=0, overall_score=0, feedback=None, recommendation="Hire"):
	"""
	Records interview feedback and updates status.
	"""
	interview = frappe.get_doc("Interview", interview_name)
	status = "Cleared" if recommendation in ["Hire", "Strong Hire", "Cleared"] else "Rejected"
	interview.status = status
	interview.flags.ignore_permissions = True
	interview.save()

	if interview.job_applicant:
		app = frappe.get_doc("Job Applicant", interview.job_applicant)
		if status == "Cleared":
			if "1" in str(interview.interview_type):
				app.status = "Round 1 Passed"
			elif "2" in str(interview.interview_type):
				app.status = "Round 2 Passed"
			elif "HR" in str(interview.interview_type):
				app.status = "Final Selection"
		else:
			app.status = "Rejected"

		comment_content = f"Interview ({interview.interview_type}) Evaluation:\nTechnical: {technical_score}/10, Communication: {communication_score}/10, Overall: {overall_score}/10\nRecommendation: {recommendation}\nFeedback: {feedback or 'N/A'}"
		frappe.get_doc({
			"doctype": "Comment",
			"comment_type": "Comment",
			"reference_doctype": "Job Applicant",
			"reference_name": app.name,
			"content": comment_content,
		}).insert(ignore_permissions=True)

		app.flags.ignore_permissions = True
		app.save()

	frappe.db.commit()

	try:
		from hrms.api.candidate_api import sync_interview_to_candidate_app, sync_job_applicant_to_candidate_app
		sync_interview_to_candidate_app(interview)
		if interview.job_applicant:
			app = frappe.get_doc("Job Applicant", interview.job_applicant)
			sync_job_applicant_to_candidate_app(app)
	except Exception:
		pass

	return interview.as_dict()


def _resolve_department(department, company="Faircode"):
	if not department:
		return None
	if frappe.db.exists("Department", department):
		return department
	dept = frappe.db.get_value("Department", {"department_name": department, "company": company}, "name")
	if dept:
		return dept
	dept = frappe.db.get_value("Department", {"department_name": department}, "name")
	if dept:
		return dept
	return department


@frappe.whitelist()
@frappe.whitelist(allow_guest=True)
def create_job_offer(applicant, designation, department, company, offer_date=None, salary=None):
	"""
	Creates Job Offer doc for selected candidate.
	"""
	existing_offer = frappe.db.get_value("Job Offer", {"job_applicant": applicant, "docstatus": ["!=", 2]}, "name")
	if existing_offer:
		return frappe.get_doc("Job Offer", existing_offer).as_dict()

	if not offer_date:
		offer_date = getdate()

	if not company:
		company = "Faircode"

	app = frappe.get_doc("Job Applicant", applicant)
	offer = frappe.new_doc("Job Offer")
	offer.job_applicant = applicant
	offer.applicant_name = app.applicant_name
	offer.applicant_email = app.email_id
	offer.designation = designation
	offer.company = company
	offer.offer_date = offer_date
	offer.status = "Accepted"
	offer.flags.ignore_permissions = True
	offer.insert()

	frappe.db.set_value("Job Applicant", applicant, "status", "Offer Accepted")

	frappe.db.commit()

	try:
		from hrms.api.candidate_api import sync_job_applicant_to_candidate_app
		sync_job_applicant_to_candidate_app(app)
	except Exception:
		pass

	return offer.as_dict()


@frappe.whitelist()
@frappe.whitelist(allow_guest=True)
def onboard_candidate_to_employee(applicant=None, job_offer=None, employee_name=None, date_of_birth="1995-05-15", gender="Male", date_of_joining=None, company=None, department=None, designation=None, personal_email=None, company_email=None):
	"""
	Converts candidate/offer into real ERPNext Employee.
	"""
	if not date_of_joining:
		date_of_joining = getdate()

	if job_offer and frappe.db.exists("Job Offer", job_offer):
		offer_doc = frappe.get_doc("Job Offer", job_offer)
		employee_name = employee_name or offer_doc.applicant_name
		designation = designation or offer_doc.designation
		department = department or offer_doc.department
		company = company or offer_doc.company
		personal_email = personal_email or offer_doc.applicant_email
		applicant = applicant or offer_doc.job_applicant

	if applicant and frappe.db.exists("Job Applicant", applicant):
		app_doc = frappe.get_doc("Job Applicant", applicant)
		employee_name = employee_name or app_doc.applicant_name
		personal_email = personal_email or app_doc.email_id
		designation = designation or app_doc.designation

	parts = (employee_name or "Employee").strip().split(" ", 1)
	first_name = parts[0]
	last_name = parts[1] if len(parts) > 1 else ""

	emp = frappe.new_doc("Employee")
	emp.first_name = first_name
	emp.last_name = last_name
	emp.employee_name = employee_name
	emp.gender = gender or "Male"
	emp.date_of_birth = date_of_birth or "1995-05-15"
	emp.date_of_joining = date_of_joining
	emp.company = company or "Faircode"
	emp.department = department or "Information Technology"
	company = company or "Faircode"
	department = _resolve_department(department or "Information Technology", company)
	emp.company = company
	emp.department = department
	emp.designation = designation or "Software Developer"
	emp.personal_email = personal_email or "unnikrishnan@example.com"
	emp.company_email = company_email or f"{first_name.lower()}@faircode.com"
	emp.status = "Active"
	emp.employment_type = "Full-time"
	emp.job_applicant = applicant
	emp.flags.ignore_permissions = True
	emp.insert()

	if applicant:
		frappe.db.set_value("Job Applicant", applicant, "status", "Employee Created")

	frappe.db.commit()
	return emp.as_dict()


@frappe.whitelist()
@frappe.whitelist(allow_guest=True)
def generate_payroll_and_slips(company, start_date, end_date, payroll_frequency="Monthly", employee=None):
	validate_not_candidate()
	"""
	Creates Payroll Entry and generates Salary Slips for the period.
	"""
	from hrms.payroll.doctype.payroll_entry.payroll_entry import get_employee_list

	comp_doc = frappe.get_doc("Company", company)
	payroll_account = comp_doc.default_payroll_payable_account or f"Payroll Payable - {comp_doc.abbr}"
	currency = comp_doc.default_currency or "INR"

	pe = frappe.new_doc("Payroll Entry")
	pe.company = company
	pe.currency = currency
	pe.payroll_payable_account = payroll_account
	pe.payroll_frequency = payroll_frequency
	pe.start_date = start_date
	pe.end_date = end_date
	pe.posting_date = end_date
	pe.exchange_rate = 1.0

	filters = frappe._dict({
		"company": company,
		"payroll_frequency": payroll_frequency,
		"start_date": start_date,
		"end_date": end_date,
		"currency": currency,
		"payroll_payable_account": payroll_account,
		"salary_slip_based_on_timesheet": 0,
	})
	try:
		emp_list = get_employee_list(filters=filters, as_dict=True, ignore_match_conditions=True)
	except Exception:
		emp_list = []

	if employee:
		emp_list = [e for e in emp_list if e.get("employee") == employee]
		if not emp_list:
			assign = frappe.db.get_value(
				"Salary Structure Assignment",
				{"employee": employee, "docstatus": 1, "from_date": ["<=", end_date]},
				["name", "salary_structure", "base"],
				as_dict=True,
				order_by="from_date desc",
			)
			if assign:
				emp_doc = frappe.get_doc("Employee", employee)
				emp_list = [{
					"employee": employee,
					"employee_name": emp_doc.employee_name,
					"department": emp_doc.department,
					"designation": emp_doc.designation,
				}]

	# Filter out any employees who already have a draft or submitted Salary Slip for this period
	existing_slip_employees = frappe.get_all(
		"Salary Slip",
		filters={
			"company": company,
			"start_date": start_date,
			"end_date": end_date,
			"docstatus": ["!=", 2],
		},
		pluck="employee",
	)
	emp_list = [e for e in emp_list if e.get("employee") not in existing_slip_employees]

	if not emp_list:
		# If any salary slips for this period are still in Draft, submit them
		draft_slips = frappe.get_all(
			"Salary Slip",
			filters={"company": company, "start_date": start_date, "end_date": end_date, "docstatus": 0},
			pluck="name",
		)
		for ds_name in draft_slips:
			try:
				ds_doc = frappe.get_doc("Salary Slip", ds_name)
				ds_doc.flags.ignore_permissions = True
				ds_doc.submit()
			except Exception as e:
				frappe.log_error(title="Submit Draft Slip Warning", message=str(e))
		frappe.db.commit()

		# All eligible employees for this period are already payrolled
		slips = frappe.get_all(
			"Salary Slip",
			filters={"company": company, "start_date": start_date, "end_date": end_date, "docstatus": ["!=", 2]},
			fields=["name", "employee", "employee_name", "gross_pay", "total_deduction", "net_pay", "status", "docstatus"],
			order_by="posting_date desc, creation desc",
		)
		existing_pe = frappe.db.get_value(
			"Payroll Entry",
			{"company": company, "start_date": start_date, "end_date": end_date, "docstatus": 1},
			"name",
			order_by="creation desc",
		) or frappe.db.get_value(
			"Payroll Entry",
			{"company": company, "start_date": start_date, "end_date": end_date},
			"name",
			order_by="creation desc",
		)
		return {
			"payroll_entry": existing_pe or "Processed",
			"number_of_employees": len(slips),
			"salary_slips": slips,
			"message": f"Salary slips for all {len(slips)} employees have already been generated for this period.",
			"message": f"Salary slips for all {len(slips)} employees have been generated and submitted for this period.",
		}

	for e in emp_list:
		pe.append("employees", e)

	pe.number_of_employees = len(pe.employees)
	pe.flags.ignore_permissions = True
	pe.insert()
	frappe.db.commit()

	try:
		pe.reload()
		pe.submit()
		frappe.db.commit()
	except Exception as exc:
		frappe.db.rollback()
		frappe.log_error(title="Payroll Entry Submit Warning", message=str(exc))

	# Automatically submit all created salary slips so none remain in Draft status
	try:
		pe.reload()
		pe.submit_salary_slips()
		frappe.db.commit()
	except Exception as exc:
		frappe.log_error(title="Payroll Entry Salary Slips Submit Warning", message=str(exc))

	slips = frappe.get_all(
		"Salary Slip",
		filters={"payroll_entry": pe.name},
		fields=["name", "employee", "employee_name", "gross_pay", "total_deduction", "net_pay", "status", "docstatus"],
		order_by="posting_date desc, creation desc",
	)
	if not slips:
		slips = frappe.get_all(
			"Salary Slip",
			filters={"company": company, "start_date": start_date, "end_date": end_date, "docstatus": ["!=", 2]},
			fields=["name", "employee", "employee_name", "gross_pay", "total_deduction", "net_pay", "status", "docstatus"],
			order_by="posting_date desc, creation desc",
		)

	frappe.db.commit()
	return {
		"payroll_entry": pe.name,
		"number_of_employees": pe.number_of_employees,
		"salary_slips": slips,
	}


@frappe.whitelist()
def submit_salary_slip(name):
	"""
	Submits an individual draft Salary Slip in ERPNext.
	"""
	validate_not_candidate()
	if not frappe.db.exists("Salary Slip", name):
		frappe.throw(_("Salary Slip {0} not found.").format(name), frappe.DoesNotExistError)

	doc = frappe.get_doc("Salary Slip", name)
	if doc.docstatus == 0:
		doc.flags.ignore_permissions = True
		doc.submit()
		frappe.db.commit()

	return doc.as_dict()


@frappe.whitelist()
def submit_all_draft_salary_slips(company="Faircode", payroll_entry=None):
	"""
	Submits all draft Salary Slips in ERPNext.
	"""
	validate_not_candidate()
	filters = {"docstatus": 0}
	if company:
		filters["company"] = company
	if payroll_entry:
		filters["payroll_entry"] = payroll_entry

	draft_slips = frappe.get_all("Salary Slip", filters=filters, pluck="name")
	submitted = []
	failed = []

	for sname in draft_slips:
		try:
			doc = frappe.get_doc("Salary Slip", sname)
			doc.flags.ignore_permissions = True
			doc.submit()
			submitted.append(sname)
		except Exception as e:
			frappe.log_error(title=f"Failed to submit salary slip {sname}", message=str(e))
			failed.append(sname)

	frappe.db.commit()
	return {
		"submitted_count": len(submitted),
		"failed_count": len(failed),
		"submitted": submitted,
		"failed": failed,
		"message": f"Successfully submitted {len(submitted)} Salary Slip(s).",
	}


@frappe.whitelist()
@frappe.whitelist(allow_guest=True)
def approve_leave_application(name):
	"""
	Approves a Leave Application and submits it in ERPNext.
	"""
	doc = frappe.get_doc("Leave Application", name)
	doc.status = "Approved"
	doc.flags.ignore_permissions = True
	if doc.docstatus == 0:
		doc.submit()
	else:
		doc.save()
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist()
@frappe.whitelist(allow_guest=True)
def reject_leave_application(name):
	"""
	Rejects a Leave Application in ERPNext.
	"""
	doc = frappe.get_doc("Leave Application", name)
	doc.status = "Rejected"
	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist()
@frappe.whitelist(allow_guest=True)
def update_employee_status(employee, status="Active"):
	"""
	Updates the employee status in ERPNext (Active, Left, Suspended).
	"""
	doc = frappe.get_doc("Employee", employee)
	doc.status = status
	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist(allow_guest=True)
@frappe.whitelist()
def register_employee_user(
	first_name,
	last_name="",
	email=None,
	username=None,
	password=None,
	department="Information Technology - F",
	designation="Software Developer",
	phone=None,
	gender="Male",
	date_of_birth="1995-01-01",
):
	"""
	Registers a new standard employee user:
	Provisions a new standard employee user by Admin/HR:
	- Only Admin/HR Manager can invoke this endpoint (never public guest)
	- Creates User with strictly 'Employee' and 'Employee Self Service' roles (never Admin)
	- Creates linked Employee record in ERPNext
	- Creates User Permission restricting the user solely to their own Employee record
	- Allocates standard 12-day Casual Leave
	- Assigns default 36 Hour Shift
	"""
	current_user = frappe.session.user
	if current_user != "Administrator":
		roles = set(frappe.get_roles(current_user))
		if not (roles & {"System Manager", "Administrator", "HR Manager"}):
			frappe.local.response["http_status_code"] = 403
			raise frappe.PermissionError(_("Permission Denied: Public employee signup is disabled. Only Admin/HR can provision employees."))

	if not first_name or not email or not username or not password:
		frappe.throw(_("First Name, Email, Username, and Password are required."))

	email = email.strip().lower()
	username = username.strip().lower()

	# Check if user already exists
	if frappe.db.exists("User", email):
		frappe.throw(_("User with email {0} already exists.").format(email))
	if frappe.db.exists("User", {"username": username}):
		frappe.throw(_("Username {0} is already taken.").format(username))

	company = frappe.db.get_single_value("Global Defaults", "default_company") or "Faircode"

	# Ensure Designation exists
	if designation and not frappe.db.exists("Designation", designation):
		try:
			d = frappe.new_doc("Designation")
			d.designation_name = designation
			d.flags.ignore_permissions = True
			d.insert(ignore_permissions=True)
		except Exception:
			pass

	# Ensure Department exists
	if department and not frappe.db.exists("Department", department):
		try:
			dp = frappe.new_doc("Department")
			dp.department_name = department
			dp.company = company
			dp.flags.ignore_permissions = True
			dp.insert(ignore_permissions=True)
		except Exception:
			pass

	# 1. Create User
	user = frappe.new_doc("User")
	user.email = email
	user.first_name = first_name.strip()
	user.last_name = (last_name or "").strip()
	user.username = username
	user.new_password = password
	user.send_welcome_email = 0
	user.enabled = 1
	user.flags.ignore_permissions = True
	user.insert(ignore_permissions=True)

	# 2. Create Employee FIRST so validate_employee_role hook finds mapped employee
	emp = frappe.new_doc("Employee")
	emp.first_name = first_name.strip()
	emp.last_name = (last_name or "").strip()
	emp.employee_name = f"{first_name} {last_name or ''}".strip()
	emp.gender = gender or "Male"
	emp.date_of_birth = date_of_birth or "1995-01-01"
	emp.user_id = email
	emp.company_email = email
	emp.personal_email = email
	emp.cell_number = phone or ""
	emp.company = company
	emp.department = department or "Information Technology - F"
	emp.designation = designation or "Software Developer"
	emp.date_of_joining = frappe.utils.today()
	emp.status = "Active"
	emp.employment_type = "Full-time"
	emp.flags.ignore_permissions = True
	emp.insert(ignore_permissions=True)

	# 3. Now assign Employee roles to User
	user.reload()
	user.append("roles", {"role": "Employee"})
	user.append("roles", {"role": "Employee Self Service"})
	user.flags.ignore_permissions = True
	user.save(ignore_permissions=True)

	# 3. Create User Permissions (Strict Data Isolation) if not already created by ERPNext
	if not frappe.db.exists("User Permission", {"user": email, "allow": "Employee", "for_value": emp.name}):
		try:
			perm = frappe.new_doc("User Permission")
			perm.user = email
			perm.allow = "Employee"
			perm.for_value = emp.name
			perm.apply_to_all_doctypes = 1
			perm.flags.ignore_permissions = True
			perm.insert(ignore_permissions=True)
		except Exception:
			pass

	if not frappe.db.exists("User Permission", {"user": email, "allow": "Company", "for_value": company}):
		try:
			perm_company = frappe.new_doc("User Permission")
			perm_company.user = email
			perm_company.allow = "Company"
			perm_company.for_value = company
			perm_company.apply_to_all_doctypes = 1
			perm_company.flags.ignore_permissions = True
			perm_company.insert(ignore_permissions=True)
		except Exception:
			pass

	# 4. Standard Leave Allocation (12 Days Casual Leave)
	try:
		if frappe.db.exists("Leave Type", "Casual Leave"):
			alloc = frappe.new_doc("Leave Allocation")
			alloc.employee = emp.name
			alloc.leave_type = "Casual Leave"
			alloc.from_date = f"{frappe.utils.today()[:4]}-01-01"
			alloc.to_date = f"{frappe.utils.today()[:4]}-12-31"
			alloc.new_leaves_allocated = 12
			alloc.docstatus = 1
			alloc.flags.ignore_permissions = True
			alloc.insert(ignore_permissions=True)
	except Exception:
		pass

	# 5. Shift Assignment (36 Hour Shift)
	try:
		if frappe.db.exists("Shift Type", "36 Hour Shift"):
			shift = frappe.new_doc("Shift Assignment")
			shift.employee = emp.name
			shift.shift_type = "36 Hour Shift"
			shift.start_date = frappe.utils.today()
			shift.company = company
			shift.status = "Active"
			shift.docstatus = 1
			shift.flags.ignore_permissions = True
			shift.insert(ignore_permissions=True)
	except Exception:
		pass

	# 6. Salary Structure Assignment
	try:
		if frappe.db.exists("Salary Structure", "Standard Monthly Salary Structure"):
			if not frappe.db.exists("Salary Structure Assignment", {"employee": emp.name, "docstatus": 1}):
				ssa = frappe.new_doc("Salary Structure Assignment")
				ssa.employee = emp.name
				ssa.salary_structure = "Standard Monthly Salary Structure"
				ssa.from_date = f"{frappe.utils.today()[:4]}-01-01"
				ssa.base = 35000
				ssa.company = company
				ssa.docstatus = 1
				ssa.flags.ignore_permissions = True
				ssa.insert(ignore_permissions=True)
	except Exception:
		pass

	frappe.db.commit()

	return {
		"success": True,
		"employee_id": emp.name,
		"employee_name": emp.employee_name,
		"user": user.name,
		"username": username,
		"email": email,
		"message": _("Employee registered successfully."),
	}


@frappe.whitelist()
def get_employee_leave_balances(employee=None):
	"""
	Returns verified leave balances for the authenticated employee directly from ERPNext leave engine.
	Restricted strictly to the session employee (or Admin).
	"""
	employee = validate_employee_access(employee)

	from frappe.utils import today
	from hrms.hr.doctype.leave_application.leave_application import get_leave_balance_on

	allocations = frappe.get_all(
		"Leave Allocation",
		filters={"employee": employee, "docstatus": 1},
		fields=["name", "leave_type", "total_leaves_allocated", "new_leaves_allocated", "from_date", "to_date"],
		order_by="from_date desc"
	)

	balances = []
	seen_types = set()
	for alloc in allocations:
		lt = alloc.leave_type
		if lt in seen_types:
			continue
		seen_types.add(lt)
		bal = 0.0
		try:
			bal = get_leave_balance_on(employee, lt, today())
		except Exception:
			tot = alloc.total_leaves_allocated or alloc.new_leaves_allocated or 0.0
			used = frappe.db.sql("""
				SELECT COALESCE(SUM(total_leave_days), 0)
				FROM `tabLeave Application`
				WHERE employee = %s AND leave_type = %s AND status = 'Approved' AND docstatus = 1
			""", (employee, lt))[0][0]
			bal = max(0.0, float(tot) - float(used))

		balances.append({
			"leave_type": lt,
			"balance": float(bal),
			"total_allocated": float(alloc.total_leaves_allocated or alloc.new_leaves_allocated or 0.0),
			"from_date": str(alloc.from_date),
			"to_date": str(alloc.to_date)
		})

	return balances



