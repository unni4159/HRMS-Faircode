# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class Candidate(Document):
	def validate(self):
		if not self.candidate_name and (self.first_name or self.last_name):
			self.candidate_name = f"{self.first_name or ''} {self.last_name or ''}".strip()

