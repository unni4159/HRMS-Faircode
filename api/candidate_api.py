# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import json
import frappe
from frappe import _
from frappe.utils import now_datetime, today, getdate
from frappe.utils.password import update_password


def is_admin_user(user=None):
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	user_roles = set(frappe.get_roles(user))
	return bool(user_roles & {"Administrator", "System Manager", "HR Manager"})


@frappe.whitelist(allow_guest=True)
def register_candidate_user(first_name=None, last_name=None, username=None, email=None, password=None, confirm_password=None, phone=None, full_name=None):
	"""
	Registers a new Candidate User with strictly the 'Candidate' role (no Admin privileges).
	Creates:
	  1. ERPNext User account (linked by email)
	  2. ERPNext Job Applicant record (in Recruitment)
	  3. ERPNext Candidate record (Candidate ID, Name, Email, Phone, Status, User link)
	Validates mandatory fields: first_name, last_name, email, phone (at least 10 digits), password (at least 6 chars), confirm_password matching.
	Enforces duplicate checking returning: 'An account with this email already exists. Please sign in.'
	Enforces transaction safety with rollback on failure.
	"""
	import re

	# Handle full_name fallback if first_name is not provided
	if not first_name and full_name:
		parts = full_name.strip().split(" ", 1)
		first_name = parts[0]
		last_name = parts[1] if len(parts) > 1 else ""

	if not first_name or not str(first_name).strip():
		frappe.throw(_("First Name is required."))

	if not last_name or not str(last_name).strip():
		frappe.throw(_("Last Name is required."))

	# Phone validation
	if not phone or not str(phone).strip():
		frappe.throw(_("Contact Number is required."))
	phone = str(phone).strip()
	digits = re.sub(r"\D", "", phone)
	if len(digits) < 10:
		frappe.throw(_("Please enter a valid contact number (at least 10 digits)."))

	if not email or not str(email).strip():
		frappe.throw(_("Email Address is required."))

	email = str(email).strip().lower()
	email_regex = r"^[\w\.-]+@[\w\.-]+\.\w+$"
	if not re.match(email_regex, email):
		frappe.throw(_("Please enter a valid email address."))

	if frappe.db.exists("User", email) or frappe.db.exists("Candidate", {"email": email}):
		frappe.throw(_("An account with this email already exists. Please sign in."))

	# Username validation
	if username and str(username).strip():
		username = str(username).strip()
		if len(username) < 3:
			frappe.throw(_("Username must be at least 3 characters long."))
		if frappe.db.exists("User", {"username": username}):
			frappe.throw(_("Username already taken. Please choose another username."))
	else:
		username = email

	# Password validation
	if not password:
		frappe.throw(_("Password is required."))
	if len(password) < 6:
		frappe.throw(_("Password must be at least 6 characters long."))
	if not confirm_password:
		frappe.throw(_("Please confirm your password."))
	if password != confirm_password:
		frappe.throw(_("Passwords do not match."))

	resolved_full_name = f"{first_name.strip()} {last_name.strip()}".strip()

	# Transactional creation of User, Job Applicant, and Candidate
	try:
		# 1. Create User
		user = frappe.new_doc("User")
		user.email = email
		user.first_name = first_name.strip()
		if last_name and str(last_name).strip():
			user.last_name = last_name.strip()
		if username and str(username).strip():
			user.username = username.strip()
		user.phone = phone
		if not frappe.db.exists("User", {"mobile_no": phone}):
			user.mobile_no = phone
		user.send_welcome_email = 0
		user.user_type = "System User"
		user.insert(ignore_permissions=True)

		# Assign strictly 'Candidate' and 'All' roles (never Admin)
		user.set("roles", [])
		user.append("roles", {"role": "Candidate"})
		user.append("roles", {"role": "All"})
		user.save(ignore_permissions=True)

		# Set password
		update_password(email, password)

		# 2. Create Job Applicant if not exists
		job_app_name = None
		if not frappe.db.exists("Job Applicant", {"email_id": email}):
			job_app = frappe.new_doc("Job Applicant")
			job_app.applicant_name = resolved_full_name
			job_app.email_id = email
			job_app.phone_number = phone
			job_app.status = "Open"
			job_app.insert(ignore_permissions=True)
			job_app_name = job_app.name
		else:
			job_app_name = frappe.db.get_value("Job Applicant", {"email_id": email}, "name")

		# 3. Create Candidate record
		cand = frappe.new_doc("Candidate")
		cand.candidate_name = resolved_full_name
		cand.first_name = first_name.strip()
		cand.last_name = last_name.strip() if last_name else ""
		cand.email = email
		cand.phone = phone
		cand.status = "Active"
		cand.user = email
		cand.job_applicant = job_app_name
		cand.insert(ignore_permissions=True)
		candidate_id = cand.name

		frappe.db.commit()

		return {
			"success": True,
			"candidate_id": candidate_id,
			"email": email,
			"full_name": resolved_full_name,
			"message": _("Candidate account registered successfully. You can now log in.")
		}

	except Exception as e:
		frappe.db.rollback()
		raise e


@frappe.whitelist()
def get_candidates_list():
	"""
	Returns all registered candidates from ERPNext Candidate DocType.
	Admin/HR access only.
	"""
	if not is_admin_user():
		frappe.throw(_("Not permitted to view candidates list"), frappe.PermissionError)

	candidates = frappe.get_all(
		"Candidate",
		fields=[
			"name",
			"candidate_name",
			"first_name",
			"last_name",
			"email",
			"phone",
			"status",
			"user",
			"job_applicant",
			"registration_date",
			"creation"
		],
		order_by="creation desc"
	)
	return candidates


@frappe.whitelist(allow_guest=True)
def get_active_job_openings():
	"""
	Returns all active Job Openings from ERPNext with available vacancies.
	Excludes any job openings that are Closed, have 0 planned vacancies, or where all vacancies are filled by selected candidates.
	Annotated with role deactivation status for candidate.
	"""
	raw_openings = frappe.get_all(
		"Job Opening",
		filters={"status": "Open"},
		fields=[
			"name",
			"job_title",
			"department",
			"designation",
			"location",
			"employment_type",
			"planned_vacancies",
			"description",
			"posted_on",
			"closes_on",
			"company",
			"status"
		],
		order_by="posted_on desc, creation desc"
	)

	openings = []
	for opening in raw_openings:
		# Must be Open
		if opening.get("status") != "Open":
			continue

		# Check remaining vacancies
		pv = opening.get("planned_vacancies")
		vac = opening.get("vacancies")
		target_vac = pv if pv is not None else (vac if vac is not None else 1)

		if target_vac <= 0:
			continue

		# Count actual selected / onboarded candidates for this vacancy
		selected_count = frappe.db.count(
			"Candidate Application",
			filters={
				"job_title": opening.name,
				"status": ["in", ["Selected", "Employee Created", "Onboarded"]]
			}
		)
		final_selected_count = frappe.db.count(
			"Candidate Application",
			filters={
				"job_title": opening.name,
				"final_result": "Selected"
			}
		)
		total_selected = max(selected_count, final_selected_count)

		# If vacancies were filled, sync status in ERPNext and exclude from candidate openings
		if total_selected >= target_vac:
			frappe.db.set_value("Job Opening", opening.name, {
				"status": "Closed",
				"planned_vacancies": 0,
				"vacancies": 0,
				"closed_on": frappe.utils.today()
			})
			frappe.db.commit()
			continue

		openings.append(opening)

	# If candidate is logged in, annotate openings with candidate deactivation status
	current_user = frappe.session.user
	user_email = frappe.db.get_value("User", current_user, "email") or current_user if current_user != "Guest" else None

	deactivated_roles = set()
	if current_user and current_user != "Guest":
		cand_apps = frappe.get_all(
			"Candidate Application",
			filters=[
				["Candidate Application", "user", "in", [current_user, user_email]],
			],
			fields=["job_title", "job_name", "interview_1_result", "interview_2_result", "final_result", "status"],
		)
		if not cand_apps and user_email:
			cand_apps = frappe.get_all(
				"Candidate Application",
				filters={"candidate_email": user_email},
				fields=["job_title", "job_name", "interview_1_result", "interview_2_result", "final_result", "status"],
			)

		for app in cand_apps:
			is_rejected = (
				app.get("interview_1_result") == "Rejected"
				or app.get("interview_2_result") == "Rejected"
				or app.get("final_result") == "Rejected"
				or (app.get("status") or "") in ["Interview 1 Rejected", "Interview 2 Rejected", "Final Rejected", "Rejected"]
				or "Rejected" in (app.get("status") or "")
			)
			if is_rejected:
				if app.get("job_title"):
					deactivated_roles.add(app.get("job_title"))
					desig = frappe.db.get_value("Job Opening", app.get("job_title"), "designation")
					if desig:
						deactivated_roles.add(desig.strip().lower())
				if app.get("job_name"):
					deactivated_roles.add(app.get("job_name").strip().lower())

	for opening in openings:
		is_deact = (
			opening.name in deactivated_roles
			or (opening.job_title and opening.job_title.strip().lower() in deactivated_roles)
			or (opening.designation and opening.designation.strip().lower() in deactivated_roles)
		)
		opening["is_deactivated_for_candidate"] = bool(is_deact)
		if is_deact:
			opening["deactivation_message"] = (
				"Your account is deactivated from this role because you were not selected in the interview process. "
				"You are only eligible to apply for other positions."
			)

	return openings


def _deduct_job_opening_vacancy(job_title):
	"""
	Deducts 1 vacancy from the specified Job Opening when a candidate is selected.
	If all vacancies are filled (planned_vacancies <= 0), automatically marks the opening as Closed in ERPNext.
	"""
	if not job_title or not frappe.db.exists("Job Opening", job_title):
		return
	try:
		job_opening = frappe.get_doc("Job Opening", job_title)
		pv = job_opening.planned_vacancies
		vac = job_opening.vacancies

		if pv is not None and pv > 0:
			job_opening.planned_vacancies = max(0, pv - 1)
		if vac is not None and vac > 0:
			job_opening.vacancies = max(0, vac - 1)

		# Check whether all vacancies have been filled
		is_closed = False
		if job_opening.planned_vacancies is not None:
			is_closed = (job_opening.planned_vacancies <= 0)
		elif job_opening.vacancies is not None:
			is_closed = (job_opening.vacancies <= 0)

		if is_closed:
			job_opening.planned_vacancies = 0
			job_opening.vacancies = 0
			job_opening.status = "Closed"
			job_opening.closed_on = frappe.utils.today()

		job_opening.flags.ignore_permissions = True
		job_opening.save()
		frappe.db.commit()
	except Exception as e:
		frappe.log_error(f"Error deducting vacancy for {job_title}: {e}")


def _sync_to_erpnext_interview(job_applicant, job_opening, interview_type, scheduled_on_dt, call_url, interviewer=None, status="Pending"):
	if not scheduled_on_dt or not job_applicant:
		return
	s_str = str(scheduled_on_dt)
	date_val = s_str.split(" ")[0]
	time_val = s_str.split(" ")[1][:8] if " " in s_str else "14:00:00"

	existing = frappe.db.get_value(
		"Interview",
		{"job_applicant": job_applicant, "interview_type": interview_type},
		"name"
	)
	if existing:
		frappe.db.set_value("Interview", existing, {
			"scheduled_on": date_val,
			"from_time": time_val,
			"to_time": "15:00:00",
			"resume_link": call_url or "https://meet.google.com/home",
			"status": status,
		})
	else:
		int_doc = frappe.new_doc("Interview")
		int_doc.job_applicant = job_applicant
		int_doc.job_opening = job_opening
		int_doc.interview_type = interview_type
		int_doc.scheduled_on = date_val
		int_doc.from_time = time_val
		int_doc.to_time = "15:00:00"
		int_doc.status = status
		int_doc.resume_link = call_url or "https://meet.google.com/home"
		int_doc.flags.ignore_permissions = True
		int_doc.flags.in_candidate_sync = True
		int_doc.insert()


def _ensure_notification(parent_app_name, title, message, notification_type):
	exists = frappe.db.exists("Candidate Notification Item", {"parent": parent_app_name, "title": title})
	if not exists:
		doc = frappe.get_doc("Candidate Application", parent_app_name)
		doc.append("notifications", {
			"title": title,
			"message": message,
			"notification_type": notification_type,
			"is_read": 0,
			"date": now_datetime()
		})
		doc.flags.ignore_permissions = True
		doc.flags.in_candidate_sync = True
		doc.save()
		frappe.db.commit()


def _add_notification_if_not_present(cand_app, title, message, notification_type):
	for n in (cand_app.notifications or []):
		if n.title == title:
			return
	cand_app.append("notifications", {
		"title": title,
		"message": message,
		"notification_type": notification_type,
		"is_read": 0,
		"date": now_datetime()
	})


def _reconcile_candidate_app_with_erpnext(app_dict):
	"""
	Reconciles a candidate application dict with ERPNext Job Applicant and tabInterview records.
	Ensures Candidate Portal always reflects real-time ERPNext data.
	"""
	app_name = app_dict.get("name")
	if not app_name:
		return

	# If job_applicant link is missing, resolve it
	ja_name = app_dict.get("job_applicant")
	if not ja_name:
		ja_name = frappe.db.get_value(
			"Job Applicant",
			{"email_id": app_dict.get("candidate_email"), "job_title": app_dict.get("job_title")},
			"name"
		) or frappe.db.get_value(
			"Job Applicant",
			{"email_id": app_dict.get("candidate_email")},
			"name"
		)
		if ja_name:
			frappe.db.set_value("Candidate Application", app_name, "job_applicant", ja_name)
			app_dict["job_applicant"] = ja_name

	if not ja_name:
		return

	# Fetch interviews for this Job Applicant
	interviews = frappe.db.sql(
		"""SELECT name, interview_type, scheduled_on, from_time, to_time, status, resume_link
		   FROM `tabInterview`
		   WHERE job_applicant = %s
		   ORDER BY scheduled_on ASC, creation ASC""",
		(ja_name,),
		as_dict=True
	)

	for itw in interviews:
		itw_type = str(itw.interview_type)
		dt_str = f"{itw.scheduled_on} {itw.from_time or '14:00:00'}" if itw.scheduled_on else ""

		if "1" in itw_type or "Round 1" in itw_type:
			if itw.status == "Pending" and itw.scheduled_on:
				if app_dict.get("interview_1_status") != "Scheduled" or app_dict.get("interview_1_date") != dt_str:
					frappe.db.set_value("Candidate Application", app_name, {
						"interview_1_status": "Scheduled",
						"interview_1_date": dt_str,
						"interview_1_call_url": itw.resume_link or "https://meet.google.com/home",
						"status": "Interview 1 Scheduled",
						"current_interview_stage": "Interview 1",
					})
					app_dict["interview_1_status"] = "Scheduled"
					app_dict["interview_1_date"] = dt_str
					app_dict["interview_1_call_url"] = itw.resume_link or "https://meet.google.com/home"
					app_dict["status"] = "Interview 1 Scheduled"
					app_dict["current_interview_stage"] = "Interview 1"
					_ensure_notification(app_name, "Interview 1 Scheduled", f"Interview 1 is scheduled for {dt_str}. Use the Call button at the scheduled time.", "Interview")
			elif itw.status in ["Cleared", "Selected"]:
				if app_dict.get("interview_1_result") != "Selected":
					frappe.db.set_value("Candidate Application", app_name, {
						"interview_1_status": "Completed",
						"interview_1_result": "Selected",
						"status": "Interview 1 Selected",
						"interview_2_status": "Pending",
						"current_interview_stage": "Interview 2",
					})
					app_dict["interview_1_status"] = "Completed"
					app_dict["interview_1_result"] = "Selected"
					app_dict["status"] = "Interview 1 Selected"
					app_dict["interview_2_status"] = "Pending"
					app_dict["current_interview_stage"] = "Interview 2"
					_ensure_notification(app_name, "Interview 1 Cleared!", "Congratulations! You have been selected in Interview 1 and are now eligible for Interview 2.", "Result")
			elif itw.status == "Rejected":
				if app_dict.get("interview_1_result") != "Rejected":
					frappe.db.set_value("Candidate Application", app_name, {
						"interview_1_status": "Completed",
						"interview_1_result": "Rejected",
						"status": "Interview 1 Rejected",
						"interview_2_status": "Not Eligible",
						"final_result": "Rejected",
						"current_interview_stage": "Completed",
					})
					app_dict["interview_1_status"] = "Completed"
					app_dict["interview_1_result"] = "Rejected"
					app_dict["status"] = "Interview 1 Rejected"
					app_dict["interview_2_status"] = "Not Eligible"
					app_dict["final_result"] = "Rejected"
					_ensure_notification(app_name, "Interview 1 Result", "Thank you for attending Interview 1. You were not selected to proceed further.", "Result")

		elif "2" in itw_type or "Round 2" in itw_type:
			if itw.status == "Pending" and itw.scheduled_on:
				if app_dict.get("interview_2_status") != "Scheduled" or app_dict.get("interview_2_date") != dt_str:
					frappe.db.set_value("Candidate Application", app_name, {
						"interview_2_status": "Scheduled",
						"interview_2_date": dt_str,
						"interview_2_call_url": itw.resume_link or "https://meet.google.com/home",
						"status": "Interview 2 Scheduled",
						"current_interview_stage": "Interview 2",
					})
					app_dict["interview_2_status"] = "Scheduled"
					app_dict["interview_2_date"] = dt_str
					app_dict["interview_2_call_url"] = itw.resume_link or "https://meet.google.com/home"
					app_dict["status"] = "Interview 2 Scheduled"
					app_dict["current_interview_stage"] = "Interview 2"
					_ensure_notification(app_name, "Interview 2 Scheduled", f"Interview 2 is scheduled for {dt_str}. Use the Call button at the scheduled time.", "Interview")
			elif itw.status in ["Cleared", "Selected"]:
				if app_dict.get("final_result") != "Selected":
					frappe.db.set_value("Candidate Application", app_name, {
						"interview_2_status": "Completed",
						"interview_2_result": "Selected",
						"status": "Selected",
						"final_result": "Selected",
						"current_interview_stage": "Completed",
					})
					app_dict["interview_2_status"] = "Completed"
					app_dict["interview_2_result"] = "Selected"
					app_dict["status"] = "Selected"
					app_dict["final_result"] = "Selected"
					_ensure_notification(app_name, "🎉 Final Selection!", f"Congratulations {app_dict.get('candidate_name')}! You have been selected for {app_dict.get('job_name')}.", "Result")
					if app_dict.get("job_title"):
						_deduct_job_opening_vacancy(app_dict.get("job_title"))
			elif itw.status == "Rejected":
				if app_dict.get("final_result") != "Rejected":
					frappe.db.set_value("Candidate Application", app_name, {
						"interview_2_status": "Completed",
						"interview_2_result": "Rejected",
						"status": "Final Rejected",
						"final_result": "Rejected",
						"current_interview_stage": "Completed",
					})
					app_dict["interview_2_status"] = "Completed"
					app_dict["interview_2_result"] = "Rejected"
					app_dict["status"] = "Final Rejected"
					app_dict["final_result"] = "Rejected"
					_ensure_notification(app_name, "Final Result", "Thank you for participating in our interview process. You were not selected at this time.", "Result")

	# Check Job Applicant status directly
	ja_status = frappe.db.get_value("Job Applicant", ja_name, "status")
	if ja_status == "Shortlisted" and app_dict.get("status") in ["Applied", "Under Review"]:
		frappe.db.set_value("Candidate Application", app_name, {
			"status": "Shortlisted",
			"interview_1_status": "Pending",
			"current_interview_stage": "Interview 1",
		})
		app_dict["status"] = "Shortlisted"
		app_dict["interview_1_status"] = "Pending"
		app_dict["current_interview_stage"] = "Interview 1"
		_ensure_notification(app_name, "Application Shortlisted", f"Congratulations {app_dict.get('candidate_name')}! Your application for {app_dict.get('job_name')} has been shortlisted for Interview 1.", "Interview")
	elif ja_status == "Rejected" and "Rejected" not in app_dict.get("status", ""):
		frappe.db.set_value("Candidate Application", app_name, {
			"status": "Rejected",
			"final_result": "Rejected",
		})
		app_dict["status"] = "Rejected"
		app_dict["final_result"] = "Rejected"
		_ensure_notification(app_name, "Application Update", f"Dear {app_dict.get('candidate_name')}, your application for {app_dict.get('job_name')} has not been shortlisted at this stage.", "Application")


@frappe.whitelist()
def submit_candidate_application(data=None, **kwargs):
	"""
	Submits a new Candidate Application and saves it in ERPNext MariaDB.
	Validates all mandatory fields and prevents duplicate applications.
	"""
	import re

	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in or sign up before submitting an application."), frappe.AuthenticationError)

	if isinstance(data, str):
		try:
			data = json.loads(data)
		except Exception:
			data = {}
	elif not data:
		data = kwargs

	job_title = data.get("job_title")
	if not job_title:
		frappe.throw(_("Applied Job Opening is required."))

	if not frappe.db.exists("Job Opening", job_title):
		frappe.throw(_("Job Opening {0} does not exist.").format(job_title))

	# Mandatory field validations
	candidate_name = str(data.get("candidate_name") or "").strip()
	if not candidate_name:
		frappe.throw(_("Full Name is required."))

	candidate_email = str(data.get("candidate_email") or frappe.session.user).strip().lower()
	if not candidate_email or not re.match(r"^[\w\.-]+@[\w\.-]+\.\w+$", candidate_email):
		frappe.throw(_("A valid email address is required."))

	phone = str(data.get("phone") or "").strip()
	if not phone:
		frappe.throw(_("Phone number is required."))
	digits = re.sub(r"\D", "", phone)
	if len(digits) < 10:
		frappe.throw(_("Phone number must contain at least 10 digits."))

	qualification = str(data.get("qualification") or "").strip()
	if not qualification:
		frappe.throw(_("Highest Qualification is required."))

	institution = str(data.get("institution") or "").strip()
	if not institution:
		frappe.throw(_("University / Institution is required."))

	graduation_year = str(data.get("graduation_year") or "").strip()
	if not graduation_year:
		frappe.throw(_("Graduation Year is required."))

	total_experience = str(data.get("total_experience") or "").strip()
	if not total_experience:
		frappe.throw(_("Total Experience is required."))

	skills = str(data.get("skills") or "").strip()
	if not skills:
		frappe.throw(_("Key Skills & Technologies are required."))

	# Duplicate Application Checks (both in Candidate Application and ERPNext Job Applicant)
	existing_cand_app = None
	if frappe.session.user and frappe.session.user not in ["Administrator", "Guest"]:
		existing_cand_app = frappe.db.get_value(
			"Candidate Application",
			{"user": frappe.session.user, "job_title": job_title},
			"name"
		)
	if not existing_cand_app and candidate_email:
		existing_cand_app = frappe.db.get_value(
			"Candidate Application",
			{"candidate_email": candidate_email, "job_title": job_title},
			"name"
		)
	if existing_cand_app:
		frappe.throw(_("You have already submitted an application for this job opening (Application ID: {0}).").format(existing_cand_app))

	candidate_name = data.get("candidate_name") or frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user
	candidate_email = data.get("candidate_email") or frappe.session.user
	existing_job_app = frappe.db.get_value(
		"Job Applicant",
		{"email_id": candidate_email, "job_title": job_title},
		"name"
	)
	if existing_job_app:
		frappe.throw(_("You have already submitted an application for this job opening (Job Applicant ID: {0}).").format(existing_job_app))

	# Job Opening details
	job_doc = frappe.get_doc("Job Opening", job_title)

	# Role Deactivation Check: Candidate cannot apply if rejected in any interview round for this role
	prior_apps = frappe.get_all(
		"Candidate Application",
		filters=[
			["Candidate Application", "user", "in", [frappe.session.user, candidate_email]],
		],
		fields=["name", "job_title", "job_name", "status", "interview_1_result", "interview_2_result", "final_result"],
	)
	if not prior_apps:
		prior_apps = frappe.get_all(
			"Candidate Application",
			filters={"candidate_email": candidate_email},
			fields=["name", "job_title", "job_name", "status", "interview_1_result", "interview_2_result", "final_result"],
		)

	target_title = (job_doc.job_title or "").strip().lower()
	target_desig = (job_doc.designation or "").strip().lower()

	for pa in prior_apps:
		is_rej = (
			pa.get("interview_1_result") == "Rejected"
			or pa.get("interview_2_result") == "Rejected"
			or pa.get("final_result") == "Rejected"
			or (pa.get("status") or "") in ["Interview 1 Rejected", "Interview 2 Rejected", "Final Rejected", "Rejected"]
			or "Rejected" in (pa.get("status") or "")
		)
		if is_rej:
			pa_job_title = pa.get("job_title")
			pa_job_name = (pa.get("job_name") or "").strip().lower()
			pa_desig = (frappe.db.get_value("Job Opening", pa_job_title, "designation") or "").strip().lower() if pa_job_title else ""
			if (
				pa_job_title == job_title
				or (target_title and pa_job_name == target_title)
				or (target_desig and pa_desig == target_desig)
			):
				frappe.throw(
					_(
						"Your account is deactivated from applying for this role because you were not selected in the interview process. "
						"You are only eligible to apply for other available positions."
					),
					frappe.ValidationError,
				)

	# Check vacancy count and status
	vacancies = job_doc.planned_vacancies if job_doc.planned_vacancies is not None else job_doc.vacancies
	selected_count = frappe.db.count(
		"Candidate Application",
		filters={
			"job_title": job_title,
			"status": ["in", ["Selected", "Employee Created", "Onboarded"]]
		}
	)
	final_sel_count = frappe.db.count(
		"Candidate Application",
		filters={
			"job_title": job_title,
			"final_result": "Selected"
		}
	)
	total_sel = max(selected_count, final_sel_count)

	if (vacancies is not None and vacancies <= 0) or job_doc.status != "Open" or (vacancies is not None and vacancies > 0 and total_sel >= vacancies):
		frappe.throw(
			_("This job opening is no longer accepting applications because all vacancies have been filled."),
			frappe.ValidationError
		)

	job_name = job_doc.job_title or job_title
	department = job_doc.department or ""
	designation = job_doc.designation or ""

	# Create ERPNext Job Applicant record in MariaDB
	job_app = frappe.new_doc("Job Applicant")
	job_app.applicant_name = candidate_name
	job_app.email_id = candidate_email
	job_app.phone_number = phone
	job_app.job_title = job_title
	job_app.designation = designation
	job_app.status = "Open"
	job_app.highest_qualification = qualification
	job_app.institution = institution
	job_app.graduation_year = graduation_year
	job_app.total_experience = total_experience
	job_app.previous_company = data.get("current_company") or ""
	job_app.previous_designation = data.get("current_designation") or ""
	job_app.technical_skills = skills
	job_app.city = data.get("city") or "Kochi"
	job_app.state = data.get("state") or "Kerala"
	job_app.country = data.get("country") or "India"
	job_app.address = data.get("address") or ""
	job_app.cover_letter = data.get("cover_letter") or ""
	if data.get("resume"):
		job_app.resume_attachment = data.get("resume")
	job_app.flags.ignore_permissions = True
	job_app.flags.in_candidate_sync = True
	job_app.insert()

	# Create Candidate Application
	doc = frappe.new_doc("Candidate Application")
	doc.user = frappe.session.user
	doc.candidate_name = candidate_name
	doc.candidate_email = candidate_email
	doc.phone = data.get("phone") or ""
	doc.phone = phone
	doc.job_title = job_title
	doc.job_name = data.get("job_name") or frappe.db.get_value("Job Opening", job_title, "job_title") or job_title
	doc.department = data.get("department") or frappe.db.get_value("Job Opening", job_title, "department") or ""
	doc.job_name = job_name
	doc.department = department
	doc.job_applicant = job_app.name
	doc.application_date = today()
	doc.status = "Applied"
	doc.current_interview_stage = "None"

	# Professional details
	doc.qualification = data.get("qualification") or ""
	doc.institution = data.get("institution") or ""
	doc.graduation_year = data.get("graduation_year") or ""
	doc.total_experience = data.get("total_experience") or ""
	doc.relevant_experience = data.get("relevant_experience") or ""
	doc.qualification = qualification
	doc.institution = institution
	doc.graduation_year = graduation_year
	doc.total_experience = total_experience
	doc.relevant_experience = data.get("relevant_experience") or total_experience
	doc.current_company = data.get("current_company") or ""
	doc.current_designation = data.get("current_designation") or ""
	doc.expected_salary = data.get("expected_salary") or ""
	doc.notice_period = data.get("notice_period") or ""
	doc.skills = data.get("skills") or ""
	doc.skills = skills

	# Location & Address
	doc.city = data.get("city") or ""
	doc.state = data.get("state") or ""
	doc.city = data.get("city") or "Kochi"
	doc.state = data.get("state") or "Kerala"
	doc.country = data.get("country") or "India"
	doc.address = data.get("address") or ""

	# Resume & Cover letter
	doc.resume = data.get("resume") or ""
	doc.resume_name = data.get("resume_name") or ""
	doc.cover_letter = data.get("cover_letter") or ""

	# Interview defaults
	doc.interview_1_status = "Not Eligible"
	doc.interview_2_status = "Not Eligible"
	doc.final_result = "Pending"

	# Initial notification
	doc.append("notifications", {
		"title": "Application Submitted",
		"message": f"Your application for {doc.job_name} has been submitted successfully to ERPNext.",
		"notification_type": "Application",
		"is_read": 0,
		"date": now_datetime()
	})

	doc.flags.ignore_permissions = True
	doc.flags.in_candidate_sync = True
	doc.insert()
	frappe.db.commit()

	return doc.as_dict()


@frappe.whitelist()
def get_candidate_applications():
	"""
	Returns applications for current session user (or all if admin).
	Strict RBAC: Candidate sees only their own records.
	"""
	if frappe.session.user == "Guest":
		return []

	if is_admin_user():
		apps = frappe.get_all(
			"Candidate Application",
			fields=["*"],
			order_by="creation desc"
		)
	else:
		apps = frappe.get_all(
			"Candidate Application",
			filters=[
				["Candidate Application", "user", "=", frappe.session.user]
			],
			fields=["*"],
			order_by="creation desc"
		)

	# Fetch notifications & history for child table preview
	# Active reconciliation with ERPNext and fetch child records
	for a in apps:
		_reconcile_candidate_app_with_erpnext(a)
		a["notifications"] = frappe.get_all(
			"Candidate Notification Item",
			filters={"parent": a["name"]},
			fields=["title", "message", "notification_type", "is_read", "date", "idx"],
			order_by="idx desc"
		)
		a["interview_history"] = frappe.get_all(
			"Candidate Interview Log",
			filters={"parent": a["name"]},
			fields=["stage", "status", "result", "scheduled_on", "interviewer", "call_url", "completed_on", "remarks"],
			order_by="idx desc"
		)

	return apps


@frappe.whitelist()
def get_candidate_application_detail(name):
	"""
	Fetches full details of a specific application with strict authorization check.
	Candidate A cannot view Candidate B's application.
	"""
	if not frappe.db.exists("Candidate Application", name):
		frappe.throw(_("Candidate Application {0} not found.").format(name), frappe.DoesNotExistError)

	doc = frappe.get_doc("Candidate Application", name)

	if not is_admin_user():
		if doc.user != frappe.session.user and doc.candidate_email != frappe.session.user:
			frappe.throw(_("Access Denied: You do not have permission to view this application."), frappe.PermissionError)

	app_dict = doc.as_dict()
	_reconcile_candidate_app_with_erpnext(app_dict)
	# Reload doc to get any reconciled child tables
	doc.reload()
	return doc.as_dict()


@frappe.whitelist()
def admin_get_candidate_applications(filters=None):
	"""
	Admin endpoint to query and filter candidate applications.
	"""
	if not is_admin_user():
		frappe.throw(_("Permission Denied: Administrator or HR Manager access required."), frappe.PermissionError)

	parsed_filters = {}
	if isinstance(filters, str):
		try:
			parsed_filters = json.loads(filters)
		except Exception:
			pass
	elif isinstance(filters, dict):
		parsed_filters = filters

	apps = frappe.get_all(
		"Candidate Application",
		filters=parsed_filters,
		fields=["*"],
		order_by="creation desc"
	)
	return apps


@frappe.whitelist()
def admin_update_application_status(name, status, hr_remarks=None):
	"""
	Updates Candidate Application status, generates notifications, and syncs state.
	Updates Candidate Application status, generates notifications, and syncs state to ERPNext.
	"""
	if not is_admin_user():
		frappe.throw(_("Permission Denied."), frappe.PermissionError)

	doc = frappe.get_doc("Candidate Application", name)
	old_status = doc.status
	doc.status = status
	if hr_remarks:
		doc.hr_remarks = hr_remarks

	if status == "Shortlisted":
		doc.interview_1_status = "Pending"
		doc.current_interview_stage = "Interview 1"
		doc.append("notifications", {
			"title": "Application Shortlisted",
			"message": f"Congratulations {doc.candidate_name}! Your application for {doc.job_name} has been shortlisted for Interview 1.",
			"notification_type": "Interview",
			"date": now_datetime()
		})
		if doc.job_applicant:
			frappe.db.set_value("Job Applicant", doc.job_applicant, "status", "Shortlisted")
	elif status == "Rejected":
		doc.final_result = "Rejected"
		doc.append("notifications", {
			"title": "Application Update",
			"message": f"Dear {doc.candidate_name}, your application for {doc.job_name} has not been shortlisted at this stage.",
			"notification_type": "Application",
			"date": now_datetime()
		})
		if doc.job_applicant:
			frappe.db.set_value("Job Applicant", doc.job_applicant, "status", "Rejected")

	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist()
def admin_schedule_interview(name, stage="Interview 1", scheduled_on=None, interviewer=None, call_url="https://meet.google.com/home", remarks=None):
	"""
	Schedules Interview 1 or Interview 2 with date, interviewer, remarks, and Google Meet URL.
	Synchronizes with ERPNext Job Applicant and tabInterview in MariaDB.
	"""
	if not is_admin_user():
		frappe.throw(_("Permission Denied."), frappe.PermissionError)

	doc = frappe.get_doc("Candidate Application", name)

	if stage == "Interview 1":
		doc.interview_1_status = "Scheduled"
		doc.interview_1_date = scheduled_on or now_datetime()
		doc.interview_1_interviewer = interviewer or "Technical Interviewer"
		doc.interview_1_call_url = call_url or "https://meet.google.com/home"
		doc.interview_1_remarks = remarks or ""
		doc.status = "Interview 1 Scheduled"
		doc.current_interview_stage = "Interview 1"
		doc.append("notifications", {
			"title": "Interview 1 Scheduled",
			"message": f"Interview 1 is scheduled for {doc.interview_1_date}. Use the Call button at the scheduled time.",
			"notification_type": "Interview",
			"date": now_datetime()
		})
		if doc.job_applicant:
			frappe.db.set_value("Job Applicant", doc.job_applicant, "status", "Interview Round 1")
			_sync_to_erpnext_interview(doc.job_applicant, doc.job_title, "Round 1", doc.interview_1_date, doc.interview_1_call_url, interviewer=doc.interview_1_interviewer, status="Pending")

	elif stage == "Interview 2":
		if doc.interview_1_result != "Selected":
			frappe.throw(_("Cannot schedule Interview 2: Candidate has not cleared/been selected in Interview 1."))
		doc.interview_2_status = "Scheduled"
		doc.interview_2_date = scheduled_on or now_datetime()
		doc.interview_2_interviewer = interviewer or "Management Interviewer"
		doc.interview_2_call_url = call_url or "https://meet.google.com/home"
		doc.interview_2_remarks = remarks or ""
		doc.status = "Interview 2 Scheduled"
		doc.current_interview_stage = "Interview 2"
		doc.append("notifications", {
			"title": "Interview 2 Scheduled",
			"message": f"Interview 2 is scheduled for {doc.interview_2_date}. Use the Call button at the scheduled time.",
			"notification_type": "Interview",
			"date": now_datetime()
		})
		if doc.job_applicant:
			frappe.db.set_value("Job Applicant", doc.job_applicant, "status", "Interview Round 2")
			_sync_to_erpnext_interview(doc.job_applicant, doc.job_title, "Round 2", doc.interview_2_date, doc.interview_2_call_url, interviewer=doc.interview_2_interviewer, status="Pending")

	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist()
def admin_mark_interview_completed(name, stage="Interview 1", remarks=None):
	"""
	Explicitly marks Interview 1 or 2 as Completed before recording result.
	"""
	if not is_admin_user():
		frappe.throw(_("Permission Denied."), frappe.PermissionError)

	doc = frappe.get_doc("Candidate Application", name)

	if stage == "Interview 1":
		doc.interview_1_status = "Completed"
		doc.status = "Interview 1 Completed"
		if remarks:
			doc.interview_1_remarks = remarks
		doc.append("interview_history", {
			"stage": "Interview 1",
			"status": "Completed",
			"result": "Pending",
			"scheduled_on": doc.interview_1_date,
			"interviewer": doc.interview_1_interviewer,
			"call_url": doc.interview_1_call_url or "https://meet.google.com/home",
			"completed_on": now_datetime(),
			"remarks": remarks or doc.interview_1_remarks
		})
		doc.append("notifications", {
			"title": "Interview 1 Completed",
			"message": "Your Interview 1 has been completed. The evaluation result will be updated soon.",
			"notification_type": "Interview",
			"date": now_datetime()
		})
	elif stage == "Interview 2":
		doc.interview_2_status = "Completed"
		doc.status = "Interview 2 Completed"
		if remarks:
			doc.interview_2_remarks = remarks
		doc.append("interview_history", {
			"stage": "Interview 2",
			"status": "Completed",
			"result": "Pending",
			"scheduled_on": doc.interview_2_date,
			"interviewer": doc.interview_2_interviewer,
			"call_url": doc.interview_2_call_url or "https://meet.google.com/home",
			"completed_on": now_datetime(),
			"remarks": remarks or doc.interview_2_remarks
		})
		doc.append("notifications", {
			"title": "Interview 2 Completed",
			"message": "Your Interview 2 has been completed. Final results will be announced shortly.",
			"notification_type": "Interview",
			"date": now_datetime()
		})

	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist()
def admin_record_interview_result(name, stage="Interview 1", result="Selected", remarks=None):
	"""
	Records SELECT or REJECT for Interview 1 or Interview 2.
	Enforces:
	- If Interview 1 Rejected: Status -> 'Interview 1 Rejected', Candidate sees 'Rejected after Interview 1', Interview 2 is BLOCKED.
	- If Interview 1 Selected: Status -> 'Interview 1 Selected', Unlocks Interview 2.
	- If Interview 2 Selected: Status -> 'Selected', Final Result -> 'Selected'.
	- If Interview 2 Rejected: Status -> 'Final Rejected', Final Result -> 'Rejected'.
	Synchronizes result to ERPNext Job Applicant and tabInterview.
	"""
	if not is_admin_user():
		frappe.throw(_("Permission Denied."), frappe.PermissionError)

	doc = frappe.get_doc("Candidate Application", name)

	if stage == "Interview 1":
		if doc.interview_1_status != "Completed":
			doc.interview_1_status = "Completed"

		doc.interview_1_result = result
		if remarks:
			doc.interview_1_remarks = remarks

		if result == "Selected":
			doc.status = "Interview 1 Selected"
			doc.interview_2_status = "Pending"  # Unlocks Interview 2
			doc.current_interview_stage = "Interview 2"
			doc.append("interview_history", {
				"stage": "Interview 1",
				"status": "Completed",
				"result": "Selected",
				"interviewer": doc.interview_1_interviewer,
				"completed_on": now_datetime(),
				"remarks": remarks or "Selected in Interview 1"
			})
			doc.append("notifications", {
				"title": "Interview 1 Cleared!",
				"message": "Congratulations! You have been selected in Interview 1 and are now eligible for Interview 2.",
				"notification_type": "Result",
				"date": now_datetime()
			})
			if doc.job_applicant:
				frappe.db.set_value("Job Applicant", doc.job_applicant, "status", "Round 1 Passed")
				_sync_to_erpnext_interview(doc.job_applicant, doc.job_title, "Round 1", doc.interview_1_date, doc.interview_1_call_url, status="Cleared")
		else:
			doc.status = "Interview 1 Rejected"
			doc.interview_2_status = "Not Eligible"  # Never visible
			doc.final_result = "Rejected"
			doc.current_interview_stage = "Completed"
			doc.append("interview_history", {
				"stage": "Interview 1",
				"status": "Completed",
				"result": "Rejected",
				"interviewer": doc.interview_1_interviewer,
				"completed_on": now_datetime(),
				"remarks": remarks or "Rejected after Interview 1"
			})
			doc.append("notifications", {
				"title": "Interview 1 Result",
				"message": "Thank you for attending Interview 1. You were not selected in this round. Your account has been deactivated from this role, but you are eligible to apply for other positions.",
				"notification_type": "Result",
				"date": now_datetime()
			})
			if doc.job_applicant:
				frappe.db.set_value("Job Applicant", doc.job_applicant, "status", "Rejected")
				_sync_to_erpnext_interview(doc.job_applicant, doc.job_title, "Round 1", doc.interview_1_date, doc.interview_1_call_url, status="Rejected")

	elif stage == "Interview 2":
		if doc.interview_2_status != "Completed":
			doc.interview_2_status = "Completed"

		doc.interview_2_result = result
		if remarks:
			doc.interview_2_remarks = remarks

		if result == "Selected":
			already_selected = (doc.status == "Selected" or doc.final_result == "Selected" or doc.status == "Employee Created")
			doc.status = "Selected"
			doc.final_result = "Selected"
			doc.current_interview_stage = "Completed"
			doc.append("interview_history", {
				"stage": "Interview 2",
				"status": "Completed",
				"result": "Selected",
				"interviewer": doc.interview_2_interviewer,
				"completed_on": now_datetime(),
				"remarks": remarks or "Final selection confirmed"
			})
			doc.append("notifications", {
				"title": "🎉 Final Selection!",
				"message": f"Congratulations {doc.candidate_name}! You have been selected for {doc.job_name}.",
				"notification_type": "Result",
				"date": now_datetime()
			})
			if doc.job_applicant:
				frappe.db.set_value("Job Applicant", doc.job_applicant, "status", "Final Selection")
				_sync_to_erpnext_interview(doc.job_applicant, doc.job_title, "Round 2", doc.interview_2_date, doc.interview_2_call_url, status="Cleared")
			if not already_selected and doc.job_title:
				_deduct_job_opening_vacancy(doc.job_title)
		else:
			doc.status = "Final Rejected"
			doc.final_result = "Rejected"
			doc.current_interview_stage = "Completed"
			doc.append("interview_history", {
				"stage": "Interview 2",
				"status": "Completed",
				"result": "Rejected",
				"interviewer": doc.interview_2_interviewer,
				"completed_on": now_datetime(),
				"remarks": remarks or "Rejected after Interview 2"
			})
			doc.append("notifications", {
				"title": "Final Result",
				"message": "Thank you for participating in our interview process. You were not selected in this round. Your account has been deactivated from this role, but you are eligible to apply for other positions.",
				"notification_type": "Result",
				"date": now_datetime()
			})
			if doc.job_applicant:
				frappe.db.set_value("Job Applicant", doc.job_applicant, "status", "Rejected")
				_sync_to_erpnext_interview(doc.job_applicant, doc.job_title, "Round 2", doc.interview_2_date, doc.interview_2_call_url, status="Rejected")

	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist()
def candidate_mark_notification_read(name, notification_idx):
	"""
	Marks a notification as read.
	Marks a notification as read with strict ownership check to prevent ID manipulation.
	"""
	if frappe.session.user == "Guest":
		return {"success": False}

	if not is_admin_user():
		owner = frappe.db.get_value("Candidate Application", name, "user")
		email = frappe.db.get_value("Candidate Application", name, "candidate_email")
		if owner != frappe.session.user and email != frappe.session.user:
			frappe.throw(_("Permission Denied: You do not own this application."), frappe.PermissionError)

	frappe.db.sql(
		"""UPDATE `tabCandidate Notification Item`
		SET is_read = 1
		WHERE parent = %s AND idx = %s""",
		(name, notification_idx)
	)
	frappe.db.commit()
	return {"success": True}


@frappe.whitelist()
def update_candidate_profile(data=None, **kwargs):
	"""
	Allows logged-in candidate to safely update contact details, qualifications, and experience.
	Updates User doc, Candidate Application docs, and ERPNext Job Applicant docs.
	"""
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in to update your profile."), frappe.AuthenticationError)

	if isinstance(data, str):
		try:
			data = json.loads(data)
		except Exception:
			data = {}
	elif not data:
		data = kwargs

	user_doc = frappe.get_doc("User", frappe.session.user)

	first_name = data.get("first_name")
	last_name = data.get("last_name")
	phone = data.get("phone")

	if first_name and str(first_name).strip():
		user_doc.first_name = str(first_name).strip()
	if last_name is not None:
		user_doc.last_name = str(last_name).strip()
	if phone and str(phone).strip():
		user_doc.phone = str(phone).strip()
		user_doc.mobile_no = str(phone).strip()

	user_doc.flags.ignore_permissions = True
	user_doc.save()

	# Update fields in Candidate Application(s) for this user
	update_fields = {}
	for field in [
		"phone", "qualification", "institution", "graduation_year",
		"total_experience", "relevant_experience", "current_company",
		"current_designation", "expected_salary", "notice_period",
		"skills", "city", "state", "country", "address"
	]:
		if field in data and data[field] is not None:
			update_fields[field] = data[field]

	if first_name:
		full_name = f"{first_name} {last_name or ''}".strip()
		update_fields["candidate_name"] = full_name

	if update_fields:
		apps = frappe.get_all(
			"Candidate Application",
			filters={"user": frappe.session.user},
			pluck="name"
		)
		for app_name in apps:
			frappe.db.set_value("Candidate Application", app_name, update_fields)
			# Also update linked Job Applicant
			ja_name = frappe.db.get_value("Candidate Application", app_name, "job_applicant")
			if ja_name and frappe.db.exists("Job Applicant", ja_name):
				ja_updates = {}
				if "candidate_name" in update_fields:
					ja_updates["applicant_name"] = update_fields["candidate_name"]
				if "phone" in update_fields:
					ja_updates["phone_number"] = update_fields["phone"]
				if "qualification" in update_fields:
					ja_updates["highest_qualification"] = update_fields["qualification"]
				if "institution" in update_fields:
					ja_updates["institution"] = update_fields["institution"]
				if "graduation_year" in update_fields:
					ja_updates["graduation_year"] = update_fields["graduation_year"]
				if "total_experience" in update_fields:
					ja_updates["total_experience"] = update_fields["total_experience"]
				if "skills" in update_fields:
					ja_updates["technical_skills"] = update_fields["skills"]
				if "current_company" in update_fields:
					ja_updates["previous_company"] = update_fields["current_company"]
				if "current_designation" in update_fields:
					ja_updates["previous_designation"] = update_fields["current_designation"]
				if "city" in update_fields:
					ja_updates["city"] = update_fields["city"]
				if "state" in update_fields:
					ja_updates["state"] = update_fields["state"]
				if "country" in update_fields:
					ja_updates["country"] = update_fields["country"]
				if "address" in update_fields:
					ja_updates["address"] = update_fields["address"]
				if ja_updates:
					frappe.db.set_value("Job Applicant", ja_name, ja_updates)

	frappe.db.commit()
	return {
		"success": True,
		"message": _("Candidate profile updated successfully.")
	}


def sync_job_applicant_to_candidate_app(doc, method=None):
	"""
	DocType event hook for Job Applicant on_update.
	Propagates ERPNext changes directly to Candidate Application.
	"""
	if getattr(frappe.flags, "in_candidate_sync", False):
		return

	# Find Candidate Application
	app_name = frappe.db.get_value("Candidate Application", {"job_applicant": doc.name}, "name")
	if not app_name:
		app_name = frappe.db.get_value(
			"Candidate Application",
			{"candidate_email": doc.email_id, "job_title": doc.job_title},
			"name"
		) or frappe.db.get_value(
			"Candidate Application",
			{"candidate_email": doc.email_id},
			"name"
		)

	if not app_name:
		return

	frappe.flags.in_candidate_sync = True
	try:
		cand_app = frappe.get_doc("Candidate Application", app_name)
		if cand_app.job_applicant != doc.name:
			cand_app.job_applicant = doc.name

		if doc.status == "Shortlisted" and cand_app.status in ["Applied", "Under Review"]:
			cand_app.status = "Shortlisted"
			cand_app.interview_1_status = "Pending"
			cand_app.current_interview_stage = "Interview 1"
			_add_notification_if_not_present(cand_app, "Application Shortlisted", f"Congratulations {cand_app.candidate_name}! Your application for {cand_app.job_name} has been shortlisted for Interview 1.", "Interview")
		elif doc.status == "Rejected" and "Rejected" not in cand_app.status:
			cand_app.status = "Rejected"
			cand_app.final_result = "Rejected"
			_add_notification_if_not_present(cand_app, "Application Update", f"Dear {cand_app.candidate_name}, your application for {cand_app.job_name} has not been shortlisted at this stage.", "Application")
		elif doc.status == "Round 1 Passed":
			cand_app.status = "Interview 1 Selected"
			cand_app.interview_1_status = "Completed"
			cand_app.interview_1_result = "Selected"
			cand_app.interview_2_status = "Pending"
			cand_app.current_interview_stage = "Interview 2"
			_add_notification_if_not_present(cand_app, "Interview 1 Cleared!", "Congratulations! You have been selected in Interview 1 and are now eligible for Interview 2.", "Result")
		elif doc.status in ["Final Selection", "Accepted", "Offer Accepted"]:
			already_selected = (cand_app.status == "Selected" or cand_app.final_result == "Selected" or cand_app.status == "Employee Created")
			cand_app.status = "Selected"
			cand_app.interview_2_status = "Completed"
			cand_app.interview_2_result = "Selected"
			cand_app.final_result = "Selected"
			cand_app.current_interview_stage = "Completed"
			_add_notification_if_not_present(cand_app, "🎉 Final Selection!", f"Congratulations {cand_app.candidate_name}! You have been selected for {cand_app.job_name}.", "Result")
			if not already_selected and cand_app.job_title:
				_deduct_job_opening_vacancy(cand_app.job_title)

		cand_app.flags.ignore_permissions = True
		cand_app.flags.in_candidate_sync = True
		cand_app.save()
		frappe.db.commit()
	finally:
		frappe.flags.in_candidate_sync = False


def sync_interview_to_candidate_app(doc, method=None):
	"""
	DocType event hook for Interview on_update.
	Propagates ERPNext interview scheduling/evaluation to Candidate Application.
	"""
	if getattr(frappe.flags, "in_candidate_sync", False):
		return

	if not doc.job_applicant:
		return

	app_name = frappe.db.get_value("Candidate Application", {"job_applicant": doc.job_applicant}, "name")
	if not app_name:
		ja_email = frappe.db.get_value("Job Applicant", doc.job_applicant, "email_id")
		if ja_email:
			app_name = frappe.db.get_value("Candidate Application", {"candidate_email": ja_email}, "name")

	if not app_name:
		return

	frappe.flags.in_candidate_sync = True
	try:
		cand_app = frappe.get_doc("Candidate Application", app_name)
		itw_type = str(doc.interview_type)
		dt_str = f"{doc.scheduled_on} {doc.from_time or '14:00:00'}" if doc.scheduled_on else ""

		if "1" in itw_type or "Round 1" in itw_type:
			if doc.status == "Pending" and doc.scheduled_on:
				cand_app.interview_1_status = "Scheduled"
				cand_app.interview_1_date = dt_str
				cand_app.interview_1_call_url = doc.resume_link or "https://meet.google.com/home"
				cand_app.status = "Interview 1 Scheduled"
				cand_app.current_interview_stage = "Interview 1"
				_add_notification_if_not_present(cand_app, "Interview 1 Scheduled", f"Interview 1 is scheduled for {dt_str}. Use the Call button at the scheduled time.", "Interview")
			elif doc.status in ["Cleared", "Selected"]:
				cand_app.interview_1_status = "Completed"
				cand_app.interview_1_result = "Selected"
				cand_app.status = "Interview 1 Selected"
				cand_app.interview_2_status = "Pending"
				cand_app.current_interview_stage = "Interview 2"
				_add_notification_if_not_present(cand_app, "Interview 1 Cleared!", "Congratulations! You have been selected in Interview 1 and are now eligible for Interview 2.", "Result")
			elif doc.status == "Rejected":
				cand_app.interview_1_status = "Completed"
				cand_app.interview_1_result = "Rejected"
				cand_app.status = "Interview 1 Rejected"
				cand_app.interview_2_status = "Not Eligible"
				cand_app.final_result = "Rejected"
				cand_app.current_interview_stage = "Completed"
				_add_notification_if_not_present(cand_app, "Interview 1 Result", "Thank you for attending Interview 1. You were not selected to proceed further.", "Result")

		elif "2" in itw_type or "Round 2" in itw_type:
			if doc.status == "Pending" and doc.scheduled_on:
				cand_app.interview_2_status = "Scheduled"
				cand_app.interview_2_date = dt_str
				cand_app.interview_2_call_url = doc.resume_link or "https://meet.google.com/home"
				cand_app.status = "Interview 2 Scheduled"
				cand_app.current_interview_stage = "Interview 2"
				_add_notification_if_not_present(cand_app, "Interview 2 Scheduled", f"Interview 2 is scheduled for {dt_str}. Use the Call button at the scheduled time.", "Interview")
			elif doc.status in ["Cleared", "Selected"]:
				already_selected = (cand_app.status == "Selected" or cand_app.final_result == "Selected" or cand_app.status in ["Employee Created", "Onboarded"])
				cand_app.interview_2_status = "Completed"
				cand_app.interview_2_result = "Selected"
				cand_app.status = "Selected"
				cand_app.final_result = "Selected"
				cand_app.current_interview_stage = "Completed"
				_add_notification_if_not_present(cand_app, "🎉 Final Selection!", f"Congratulations {cand_app.candidate_name}! You have been selected for {cand_app.job_name}.", "Result")
				if not already_selected and cand_app.job_title:
					_deduct_job_opening_vacancy(cand_app.job_title)
			elif doc.status == "Rejected":
				cand_app.interview_2_status = "Completed"
				cand_app.interview_2_result = "Rejected"
				cand_app.status = "Final Rejected"
				cand_app.final_result = "Rejected"
				cand_app.current_interview_stage = "Completed"
				_add_notification_if_not_present(cand_app, "Final Result", "Thank you for participating in our interview process. You were not selected at this time.", "Result")

		cand_app.flags.ignore_permissions = True
		cand_app.flags.in_candidate_sync = True
		cand_app.save()
		frappe.db.commit()
	finally:
		frappe.flags.in_candidate_sync = False


@frappe.whitelist()
def align_candidate_demo_data():
	"""
	Aligns CAND-APP-2026-00005 with ERPNext Job Applicant and Interview records.
	Ensures candidate@faircode.com demo data is 100% in sync with screenshots 1, 3, and 4.
	"""
	app5 = frappe.get_doc("Candidate Application", "CAND-APP-2026-00005") if frappe.db.exists("Candidate Application", "CAND-APP-2026-00005") else None
	if app5:
		ja_name = frappe.db.get_value("Job Applicant", {"email_id": app5.candidate_email, "job_title": app5.job_title}, "name")
		if not ja_name:
			ja = frappe.new_doc("Job Applicant")
			ja.applicant_name = app5.candidate_name or "Candidate User"
			ja.email_id = app5.candidate_email
			ja.phone_number = app5.phone or "+91 98765 43210"
			ja.job_title = app5.job_title
			ja.designation = "Software Developer"
			ja.status = "Interview Round 1"
			ja.highest_qualification = app5.qualification or "B.Tech / B.E. Computer Science"
			ja.institution = app5.institution or "APJ Abdul Kalam Technological University"
			ja.graduation_year = app5.graduation_year or "2022"
			ja.total_experience = app5.total_experience or "3 Years"
			ja.technical_skills = app5.skills or "JavaScript, React, Python, ERPNext, SQL"
			ja.city = app5.city or "Kochi"
			ja.state = app5.state or "Kerala"
			ja.country = app5.country or "India"
			ja.flags.ignore_permissions = True
			ja.flags.in_candidate_sync = True
			ja.insert()
			ja_name = ja.name

		frappe.db.set_value("Candidate Application", "CAND-APP-2026-00005", "job_applicant", ja_name)

		existing_int = frappe.db.get_value("Interview", {"job_applicant": ja_name, "interview_type": "Round 1"}, "name")
		if not existing_int:
			int_doc = frappe.new_doc("Interview")
			int_doc.job_applicant = ja_name
			int_doc.job_opening = app5.job_title
			int_doc.interview_type = "Round 1"
			int_doc.scheduled_on = "2026-09-15"
			int_doc.from_time = "14:00:00"
			int_doc.to_time = "15:00:00"
			int_doc.status = "Pending"
			int_doc.resume_link = "https://meet.google.com/home"
			int_doc.flags.ignore_permissions = True
			int_doc.flags.in_candidate_sync = True
			int_doc.insert()
		else:
			frappe.db.set_value("Interview", existing_int, {
				"scheduled_on": "2026-09-15",
				"from_time": "14:00:00",
				"to_time": "15:00:00",
				"status": "Pending",
				"resume_link": "https://meet.google.com/home"
			})

	# Align CAND-APP-2026-00008
	app8 = frappe.get_doc("Candidate Application", "CAND-APP-2026-00008") if frappe.db.exists("Candidate Application", "CAND-APP-2026-00008") else None
	if app8:
		ja8_name = frappe.db.get_value("Job Applicant", {"email_id": app8.candidate_email, "job_title": app8.job_title}, "name")
		if not ja8_name:
			ja8_name = "candidate@faircode.com"
		frappe.db.set_value("Candidate Application", "CAND-APP-2026-00008", "job_applicant", ja8_name)

	frappe.db.commit()
	return {"success": True, "message": "Demo data aligned successfully."}


@frappe.whitelist()
def select_candidate_as_employee(application_name=None, candidate_id=None):
	"""
	Converts/links a selected candidate to an ERPNext Employee record.
	- Ensures idempotency: will NOT create duplicate employees or decrement vacancy twice.
	- Checks if Employee already exists for user_id, personal_email, or company_email.
	- Creates new Employee with required statutory/ERPNext fields if not existing.
	- Links existing User to Employee.
	- Updates user roles: adds Employee & Employee Self Service (without admin rights).
	- Updates Candidate Application, Candidate, and Job Applicant status to 'Employee Created' / 'Selected'.
	- Decrements Job Opening vacancies; closes Job Opening if vacancies reach 0.
	- Sends congratulatory notification to Candidate Notification Item.
	"""
	if not is_admin_user():
		frappe.throw(_("Permission Denied: Administrator or HR Manager access required."), frappe.PermissionError)

	if not application_name and not candidate_id:
		frappe.throw(_("Application name or Candidate ID is required."))

	from frappe.utils import today, now_datetime

	try:
		cand_app = None
		if application_name:
			if not frappe.db.exists("Candidate Application", application_name):
				frappe.throw(_("Candidate Application {0} does not exist.").format(application_name))
			cand_app = frappe.get_doc("Candidate Application", application_name)
		elif candidate_id:
			app_name = frappe.db.get_value("Candidate Application", {"candidate": candidate_id}, "name")
			if not app_name:
				cand_email = frappe.db.get_value("Candidate", candidate_id, "email")
				if cand_email:
					app_name = frappe.db.get_value("Candidate Application", {"candidate_email": cand_email}, "name", order_by="creation desc")
			if app_name:
				cand_app = frappe.get_doc("Candidate Application", app_name)

		if not cand_app:
			frappe.throw(_("No candidate application found to onboard."))

		candidate_email = cand_app.candidate_email
		candidate_user = cand_app.user or candidate_email
		candidate_name = cand_app.candidate_name or "Candidate User"

		# Find user doc
		user_doc = None
		if candidate_user and frappe.db.exists("User", candidate_user):
			user_doc = frappe.get_doc("User", candidate_user)
		elif candidate_email and frappe.db.exists("User", candidate_email):
			user_doc = frappe.get_doc("User", candidate_email)

		# 1. Check for existing Employee record (duplicate prevention)
		existing_emp_name = None
		if user_doc:
			existing_emp_name = frappe.db.get_value("Employee", {"user_id": user_doc.name}, "name")
		if not existing_emp_name and candidate_email:
			existing_emp_name = (
				frappe.db.get_value("Employee", {"personal_email": candidate_email}, "name")
				or frappe.db.get_value("Employee", {"company_email": candidate_email}, "name")
			)

		emp_created = False
		emp_doc = None

		if existing_emp_name:
			emp_doc = frappe.get_doc("Employee", existing_emp_name)
			if user_doc and emp_doc.user_id != user_doc.name:
				emp_doc.user_id = user_doc.name
				emp_doc.flags.ignore_permissions = True
				emp_doc.save()
		else:
			name_parts = candidate_name.strip().split(" ", 1)
			first_name = name_parts[0] if name_parts else "Employee"
			last_name = name_parts[1] if len(name_parts) > 1 else ""

			department = cand_app.department
			designation = None
			company = "Faircode"

			if cand_app.job_title and frappe.db.exists("Job Opening", cand_app.job_title):
				job_opening = frappe.get_doc("Job Opening", cand_app.job_title)
				department = department or job_opening.department
				designation = job_opening.designation
				company = job_opening.company or company

			department = department or "Information Technology - F"
			designation = designation or cand_app.job_name or "Software Developer"

			emp_doc = frappe.new_doc("Employee")
			emp_doc.naming_series = "HR-EMP-"
			emp_doc.first_name = first_name
			emp_doc.last_name = last_name
			emp_doc.employee_name = candidate_name
			emp_doc.gender = "Male"
			emp_doc.date_of_birth = "1998-01-01"
			emp_doc.date_of_joining = today()
			emp_doc.status = "Active"
			emp_doc.company = company
			emp_doc.department = department
			emp_doc.designation = designation
			emp_doc.employment_type = "Full-time"
			emp_doc.user_id = user_doc.name if user_doc else candidate_email
			emp_doc.personal_email = candidate_email
			emp_doc.company_email = candidate_email
			emp_doc.cell_number = cand_app.phone or "+91 98765 43210"

			emp_doc.flags.ignore_permissions = True
			emp_doc.insert()
			emp_created = True

		# 2. Update User roles (grant Employee & Employee Self Service)
		if user_doc:
			user_doc.reload()
			user_roles = [r.role for r in user_doc.roles]
			roles_to_add = []
			for role_name in ["Employee", "Employee Self Service"]:
				if role_name not in user_roles:
					roles_to_add.append(role_name)

			if roles_to_add:
				user_doc.add_roles(*roles_to_add)
				user_doc.reload()

			# Remove Candidate role
			user_doc.roles = [r for r in user_doc.roles if r.role != "Candidate"]
			user_doc.flags.ignore_permissions = True
			user_doc.save()

		# 3. Vacancy reduction on Job Opening (idempotent: only if not already deducted for this application)
		already_selected = (cand_app.final_result == "Selected" or cand_app.status in ["Selected", "Employee Created", "Onboarded"])
		if not already_selected and cand_app.job_title:
			_deduct_job_opening_vacancy(cand_app.job_title)

		# 4. Update Candidate Application status
		cand_app.status = "Employee Created"
		cand_app.final_result = "Selected"
		cand_app.current_interview_stage = "Completed"

		# 5. Candidate notification
		notification_msg = (
			"Congratulations! You have been selected as an employee at Faircode. "
			"Your employee account has been created successfully. "
			"You can now log in to the Employee Portal using your login credentials."
		)
		existing_notifs = [n.message for n in (cand_app.notifications or [])]
		if notification_msg not in existing_notifs:
			cand_app.append("notifications", {
				"title": "🎉 Congratulations! Selected as Employee",
				"message": notification_msg,
				"notification_type": "Result",
				"is_read": 0,
				"date": now_datetime()
			})

		cand_app.flags.ignore_permissions = True
		cand_app.flags.in_candidate_sync = True
		cand_app.save()

		# 6. Update linked Candidate DocType
		if candidate_email:
			cand_records = frappe.get_all("Candidate", filters={"email": candidate_email}, pluck="name")
			for cr in cand_records:
				frappe.db.set_value("Candidate", cr, "status", "Employee Created")

		# 7. Update linked Job Applicant DocType
		if cand_app.job_applicant and frappe.db.exists("Job Applicant", cand_app.job_applicant):
			frappe.db.set_value("Job Applicant", cand_app.job_applicant, "status", "Employee Created")

		frappe.db.commit()

		return {
			"success": True,
			"employee": emp_doc.name,
			"employee_name": emp_doc.employee_name,
			"application": cand_app.name,
			"status": "Employee Created",
			"is_newly_created": emp_created,
			"message": _("Candidate successfully onboarded as ERPNext Employee {0}.").format(emp_doc.name)
		}

	except Exception as e:
		frappe.db.rollback()
		raise e


