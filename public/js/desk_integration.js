// ERPNext Desk Integration for Faircode HRMS
// 1. Blocks normal employees and candidates from accessing Desk, redirecting them to their respective dashboards
// 2. Injects a prominent "Back to Website" button in the Desk navbar for Admin routing to /dashboard

(function() {
  function checkDeskAccess() {
    if (!window.frappe) return;
    var user = frappe.session ? frappe.session.user : null;
    if (!user || user === 'Guest') return;

    var roles = frappe.user_roles || (frappe.boot && frappe.boot.user ? frappe.boot.user.roles : []);
    var isAdmin = user === 'Administrator' || roles.indexOf('System Manager') !== -1 || roles.indexOf('HR Manager') !== -1;

    if (!isAdmin) {
      if (roles.indexOf('Candidate') !== -1) {
        console.warn('[HRMS Desk] Access denied for Candidate:', user, 'Redirecting to /candidate/dashboard');
        window.location.replace('/candidate/dashboard');
      } else {
        console.warn('[HRMS Desk] Access denied for non-admin user:', user, 'Redirecting to /user/dashboard');
        window.location.replace('/user/dashboard');
      }
    }
  }

  function getBackButtonHtml(id, extraClass, customStyle) {
    return `
      <a href="/dashboard" id="${id}" class="${extraClass || 'btn-back-to-hrms-site'}" style="display: inline-flex; align-items: center; gap: 6px; font-weight: 600; border-radius: 6px; background-color: #4f46e5; border: 1px solid #4f46e5; color: #ffffff !important; text-decoration: none; padding: 6px 14px; font-size: 13px; box-shadow: 0 1px 2px rgba(0,0,0,0.08); transition: all 0.2s; cursor: pointer; ${customStyle || ''}" title="Return to HRMS Admin Dashboard">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink: 0;">
          <path d="M19 12H5M12 19l-7-7 7-7"/>
        </svg>
        <span style="white-space: nowrap;">Back to Website</span>
      </a>
    `;
  }

  function injectBackToWebsiteButton() {
    var user = window.frappe && frappe.session ? frappe.session.user : null;
    if (!user || user === 'Guest') return;

    var roles = frappe.user_roles || (frappe.boot && frappe.boot.user ? frappe.boot.user.roles : []);
    var isAdmin = user === 'Administrator' || roles.indexOf('System Manager') !== -1 || roles.indexOf('HR Manager') !== -1;
    if (!isAdmin) {
      // Clean up if somehow present
      $('#btn-back-to-hrms-website, #btn-back-to-hrms-subpage').remove();
      return;
    }

    // 1. Desktop page navbar injection
    var $desktopNavbarRight = $('.desktop-navbar .flex[style*="align-items: center"], .desktop-navbar .flex');
    if ($desktopNavbarRight.length && !$('#btn-back-to-hrms-website').length) {
      var $notif = $desktopNavbarRight.find('.desktop-notifications');
      var btn = getBackButtonHtml('btn-back-to-hrms-website', 'btn btn-sm btn-primary flex align-center');
      if ($notif.length) {
        $(btn).insertBefore($notif);
      } else {
        $desktopNavbarRight.prepend(btn);
      }
    }

    // 2. Subpage header injection (DocType, Workspace, Report, List views)
    var $pageHead = $('.page-head:visible, .body-sidebar ~ div .page-head');
    if ($pageHead.length && !$('#btn-back-to-hrms-subpage').length && !$('#btn-back-to-hrms-website:visible').length) {
      var $actions = $pageHead.find('.page-actions, .standard-actions, .page-head-content .flex:last');
      var subpageBtn = getBackButtonHtml('btn-back-to-hrms-subpage', 'btn btn-sm btn-primary flex align-center ml-2 mr-2', 'padding: 5px 12px; font-size: 12px;');
      if ($actions.length) {
        $actions.prepend(subpageBtn);
      } else {
        $pageHead.find('.page-head-content').append(subpageBtn);
      }
    }

    // 3. User Avatar menu item on Desktop
    if (window.frappe && frappe.pages && frappe.pages['desktop'] && frappe.pages['desktop'].desktop_page) {
      var dp = frappe.pages['desktop'].desktop_page;
      if (dp.add_menu_item && !dp._has_hrms_back_item) {
        dp.add_menu_item({
          label: 'Back to HRMS Website',
          icon: 'arrow-left',
          onClick: function() {
            window.location.href = '/dashboard';
          },
          order: 1
        });
        dp._has_hrms_back_item = true;
      }
    }

    // 4. User dropdown menu fallback
    var $dropdown = $('#toolbar-user .dropdown-menu, .dropdown-navbar-user .dropdown-menu');
    if ($dropdown.length && !$('#menu-item-back-to-website').length) {
      var dropdownItem = `
        <li id="menu-item-back-to-website">
          <a class="dropdown-item d-flex align-items-center gap-2" href="/dashboard" style="color: #4f46e5; font-weight: 600;">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M19 12H5M12 19l-7-7 7-7"/>
            </svg>
            <span>Back to HRMS Website</span>
          </a>
        </li>
      `;
      $dropdown.prepend(dropdownItem);
    }
  }

  // Initial check
  checkDeskAccess();

  // Setup DOM MutationObserver to ensure buttons persist across SPA router transitions
  if (typeof MutationObserver !== 'undefined') {
    var observer = new MutationObserver(function() {
      checkDeskAccess();
      injectBackToWebsiteButton();
    });
    if (document.body) {
      observer.observe(document.body, { childList: true, subtree: true });
    } else {
      document.addEventListener('DOMContentLoaded', function() {
        observer.observe(document.body, { childList: true, subtree: true });
      });
    }
  }

  // Event bindings
  if (typeof $ !== 'undefined') {
    $(document).ready(function() {
      checkDeskAccess();
      injectBackToWebsiteButton();
    });

    $(document).on('toolbar_setup app_ready page-change route', function() {
      checkDeskAccess();
      injectBackToWebsiteButton();
    });
  }

  // Fallback timer intervals
  var intervals = [300, 800, 1500, 3000];
  intervals.forEach(function(delay) {
    setTimeout(function() {
      checkDeskAccess();
      injectBackToWebsiteButton();
    }, delay);
  });
})();
