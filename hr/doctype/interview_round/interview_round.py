# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt


import json

import frappe
from frappe.model.document import Document


class InterviewRound(Document):
	pass


@frappe.whitelist()
def create_interview(interview_round: str) -> Document:
	doc = frappe.get_doc("Interview Round", interview_round)

	interview = frappe.new_doc("Interview")
	interview.interview_round = doc.name
	interview.designation = doc.designation

	if doc.interviewers:
		interview.interview_details = []
		for d in doc.interviewers:
			interview.append("interview_details", {"interviewer": d.user})

	return interview
