import frappe
from frappe.boot import load_translations

no_cache = 1


def get_context(context):
	current_user = frappe.session.user
	if current_user not in ("Administrator", "Guest"):
		roles = set(frappe.get_roles(current_user))
		is_admin = bool(roles & {"System Manager", "Administrator", "HR Manager"})
		is_candidate = "Candidate" in roles and not is_admin
		is_employee = not is_admin and not is_candidate

		path = frappe.request.path or ""
		if is_candidate:
			if path.startswith("/user") or path.startswith("/admin") or path == "/dashboard":
				frappe.redirect("/candidate/dashboard")
		elif is_employee:
			if path.startswith("/candidate") or path.startswith("/admin") or path == "/dashboard":
				frappe.redirect("/user/dashboard")

	csrf_token = frappe.sessions.get_csrf_token()
	frappe.db.commit()  # nosempgrep
	context = frappe._dict()
	context.csrf_token = csrf_token
	context.boot = get_boot()
	context.site_name = frappe.local.site
	return context


@frappe.whitelist(methods=["POST"], allow_guest=True)
def get_context_for_dev():
	if not frappe.conf.developer_mode:
		frappe.throw(frappe._("This method is only meant for developer mode"))
	return get_boot()


def get_boot():
	bootinfo = frappe._dict(
		{
			"site_name": frappe.local.site,
			"socketio_port": frappe.conf.get("socketio_port") or 9000,
			"push_relay_server_url": frappe.conf.get("push_relay_server_url") or "",
			"default_route": get_default_route(),
		}
	)

	bootinfo.lang = frappe.local.lang
	load_translations(bootinfo)

	return bootinfo


def get_default_route():
	return "/hrms"
