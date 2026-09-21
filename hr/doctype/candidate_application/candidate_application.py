# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime, getdate, today


class CandidateApplication(Document):
	def validate(self):
		if self.job_title and not self.job_name:
			self.job_name = frappe.db.get_value("Job Opening", self.job_title, "job_title") or self.job_title

		if not self.application_date:
			self.application_date = today()

		# Ensure consistent interview stages
		if self.status in ["Shortlisted", "Interview 1 Scheduled", "Interview 1 Completed", "Interview 1 Rejected"]:
			self.current_interview_stage = "Interview 1"
		elif self.status in ["Interview 1 Selected", "Interview 2 Scheduled", "Interview 2 Completed", "Interview 2 Selected", "Interview 2 Rejected"]:
			self.current_interview_stage = "Interview 2"
		elif self.status in ["Selected", "Final Rejected"]:
			self.current_interview_stage = "Completed"

		# If shortlisted and interview_1_status is not eligible, make it pending
		if self.status == "Shortlisted" and self.interview_1_status == "Not Eligible":
			self.interview_1_status = "Pending"

		# Default call URLs
		if not self.interview_1_call_url:
			self.interview_1_call_url = "https://meet.google.com/home"
		if not self.interview_2_call_url:
			self.interview_2_call_url = "https://meet.google.com/home"

	def on_update(self):
		# Sync to ERPNext Job Applicant for backward compatibility
		if getattr(frappe.flags, "in_candidate_sync", False):
			return

		frappe.flags.in_candidate_sync = True
		try:
			self.sync_to_job_applicant()
		except Exception as e:
			frappe.log_error(f"Failed to sync to Job Applicant: {e}", "Candidate Application Sync")
		finally:
			frappe.flags.in_candidate_sync = False

	def sync_to_job_applicant(self):
		if not self.candidate_email:
			return

		applicant_name = frappe.db.get_value("Job Applicant", {"email_id": self.candidate_email}, "name")
		if applicant_name:
			doc = frappe.get_doc("Job Applicant", applicant_name)
		job_app = None
		if self.job_applicant and frappe.db.exists("Job Applicant", self.job_applicant):
			job_app = frappe.get_doc("Job Applicant", self.job_applicant)
		else:
			doc = frappe.new_doc("Job Applicant")
			doc.applicant_name = self.candidate_name
			doc.email_id = self.candidate_email
			existing_name = frappe.db.get_value(
				"Job Applicant",
				{"email_id": self.candidate_email, "job_title": self.job_title},
				"name"
			) or frappe.db.get_value(
				"Job Applicant",
				{"email_id": self.candidate_email},
				"name"
			)
			if existing_name:
				job_app = frappe.get_doc("Job Applicant", existing_name)
			else:
				job_app = frappe.new_doc("Job Applicant")
				job_app.applicant_name = self.candidate_name
				job_app.email_id = self.candidate_email

		doc.phone_number = self.phone or ""
		doc.job_title = self.job_title
		doc.cover_letter = self.cover_letter or ""
		job_app.applicant_name = self.candidate_name or job_app.applicant_name
		job_app.email_id = self.candidate_email
		job_app.phone_number = self.phone or job_app.phone_number or ""
		job_app.job_title = self.job_title or job_app.job_title
		if not job_app.designation and self.job_title:
			job_app.designation = frappe.db.get_value("Job Opening", self.job_title, "designation") or ""

		# Map status
		job_app.highest_qualification = self.qualification or job_app.highest_qualification or ""
		job_app.institution = self.institution or job_app.institution or ""
		job_app.graduation_year = self.graduation_year or job_app.graduation_year or ""
		job_app.total_experience = self.total_experience or job_app.total_experience or ""
		job_app.technical_skills = self.skills or job_app.technical_skills or ""
		job_app.previous_company = self.current_company or job_app.previous_company or ""
		job_app.previous_designation = self.current_designation or job_app.previous_designation or ""
		job_app.city = self.city or job_app.city or ""
		job_app.state = self.state or job_app.state or ""
		job_app.country = self.country or job_app.country or "India"
		job_app.address = self.address or job_app.address or ""
		job_app.cover_letter = self.cover_letter or job_app.cover_letter or ""
		if self.resume:
			job_app.resume_attachment = self.resume

		# Map status to ERPNext Job Applicant
		if self.status in ["Applied", "Under Review"]:
			doc.status = "Application Received"
			job_app.status = "Open"
		elif self.status == "Shortlisted":
			doc.status = "Shortlisted"
			job_app.status = "Shortlisted"
		elif self.status in ["Interview 1 Scheduled", "Interview 1 Completed"]:
			doc.status = "Interview Round 1"
			job_app.status = "Interview Round 1"
		elif self.status == "Interview 1 Selected":
			doc.status = "Round 1 Passed"
			job_app.status = "Round 1 Passed"
		elif self.status in ["Interview 2 Scheduled", "Interview 2 Completed"]:
			doc.status = "Interview Round 2"
			job_app.status = "Interview Round 2"
		elif self.status in ["Interview 2 Selected", "Selected"]:
			doc.status = "Accepted"
			job_app.status = "Final Selection"
		elif "Rejected" in self.status:
			doc.status = "Rejected"
			job_app.status = "Rejected"

		doc.flags.ignore_permissions = True
		doc.save()
		job_app.flags.ignore_permissions = True
		job_app.flags.in_candidate_sync = True
		job_app.save()

		if self.job_applicant != job_app.name:
			self.db_set("job_applicant", job_app.name)

		# Sync Interview 1 to tabInterview
		if self.interview_1_status == "Scheduled" and self.interview_1_date:
			self._sync_interview_record(
				job_applicant=job_app.name,
				interview_type="Round 1",
				scheduled_on=self.interview_1_date,
				call_url=self.interview_1_call_url,
				interviewer=self.interview_1_interviewer,
				status="Pending"
			)
		elif self.interview_1_status == "Completed":
			res_status = "Cleared" if self.interview_1_result == "Selected" else ("Rejected" if self.interview_1_result == "Rejected" else "Completed")
			self._sync_interview_record(
				job_applicant=job_app.name,
				interview_type="Round 1",
				scheduled_on=self.interview_1_date,
				call_url=self.interview_1_call_url,
				interviewer=self.interview_1_interviewer,
				status=res_status
			)

		# Sync Interview 2 to tabInterview
		if self.interview_2_status == "Scheduled" and self.interview_2_date:
			self._sync_interview_record(
				job_applicant=job_app.name,
				interview_type="Round 2",
				scheduled_on=self.interview_2_date,
				call_url=self.interview_2_call_url,
				interviewer=self.interview_2_interviewer,
				status="Pending"
			)
		elif self.interview_2_status == "Completed":
			res_status = "Cleared" if self.interview_2_result == "Selected" else ("Rejected" if self.interview_2_result == "Rejected" else "Completed")
			self._sync_interview_record(
				job_applicant=job_app.name,
				interview_type="Round 2",
				scheduled_on=self.interview_2_date,
				call_url=self.interview_2_call_url,
				interviewer=self.interview_2_interviewer,
				status=res_status
			)

	def _sync_interview_record(self, job_applicant, interview_type, scheduled_on, call_url, interviewer=None, status="Pending"):
		if not scheduled_on:
			return

		s_str = str(scheduled_on)
		date_val = s_str.split(" ")[0]
		time_val = s_str.split(" ")[1][:8] if " " in s_str else "14:00:00"

		existing_int = frappe.db.get_value(
			"Interview",
			{"job_applicant": job_applicant, "interview_type": interview_type},
			"name"
		)

		if existing_int:
			frappe.db.set_value("Interview", existing_int, {
				"scheduled_on": date_val,
				"from_time": time_val,
				"to_time": "15:00:00",
				"resume_link": call_url or "https://meet.google.com/home",
				"status": status,
			})
		else:
			int_doc = frappe.new_doc("Interview")
			int_doc.job_applicant = job_applicant
			int_doc.job_opening = self.job_title
			int_doc.interview_type = interview_type
			int_doc.scheduled_on = date_val
			int_doc.from_time = time_val
			int_doc.to_time = "15:00:00"
			int_doc.status = status
			int_doc.resume_link = call_url or "https://meet.google.com/home"
			int_doc.flags.ignore_permissions = True
			int_doc.flags.in_candidate_sync = True
			int_doc.insert()
