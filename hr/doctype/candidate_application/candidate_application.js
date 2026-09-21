// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Candidate Application', {
	refresh: function(frm) {
		if (frm.doc.status === 'Applied' || frm.doc.status === 'Under Review') {
			frm.add_custom_button(__('Shortlist for Interview 1'), function() {
				frappe.call({
					method: 'hrms.api.candidate_api.admin_update_application_status',
					args: {
						name: frm.doc.name,
						status: 'Shortlisted',
						hr_remarks: 'Shortlisted from ERPNext Desk'
					},
					callback: function() {
						frm.reload_doc();
					}
				});
			}, __('Actions'));

			frm.add_custom_button(__('Reject Application'), function() {
				frappe.call({
					method: 'hrms.api.candidate_api.admin_update_application_status',
					args: {
						name: frm.doc.name,
						status: 'Rejected',
						hr_remarks: 'Rejected from ERPNext Desk'
					},
					callback: function() {
						frm.reload_doc();
					}
				});
			}, __('Actions'));
		}
	}
});

