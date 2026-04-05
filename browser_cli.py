#!/usr/bin/env python3
"""
Visionless Browser CLI — Controls your REAL Chrome via Playwright + CDP.
Includes ALL Playwright MCP features adapted for CLI use.

Usage:
  1. Launch Chrome with:  open -a "Google Chrome" --args --remote-debugging-port=9222
  2. Run this script:     python3 browser_cli.py
"""

import sys
import cmd
import json
import time
import os
import datetime
import subprocess

from playwright.sync_api import sync_playwright # type: ignore
from typing import Any


# ─── JS that builds the full accessibility snapshot ──────────────────────────

JS_SNAPSHOT = r"""
(function() {
    let idCounter = 1;
    const items = [];
    const seen = new WeakSet();

    document.querySelectorAll('[data-cli-id]').forEach(el => el.removeAttribute('data-cli-id'));

    const INTERACTIVE_TAGS = new Set(['a','button','input','textarea','select','details','summary','dialog','menu']);
    const INTERACTIVE_ROLES = new Set([
        'button','link','checkbox','menuitem','menuitemcheckbox','menuitemradio',
        'radio','tab','treeitem','switch','searchbox','textbox','combobox','option',
        'dialog','alertdialog','menu','menubar','listbox','gridcell','row',
        'slider','spinbutton','scrollbar','separator','progressbar',
        'navigation','toolbar','tablist','tabpanel','tooltip','alert',
        'listitem','figure','form','img','log','marquee','math','note',
        'status','timer','banner','complementary','contentinfo','main','region','search'
    ]);
    const SKIP_TAGS = new Set(['script','style','noscript','meta','head','br','hr','link']);

    function isInteractive(node) {
        const tag = node.tagName.toLowerCase();
        if (INTERACTIVE_TAGS.has(tag)) return true;

        // ARIA roles
        const role = (node.getAttribute('role') || '').toLowerCase();
        if (role && INTERACTIVE_ROLES.has(role)) return true;

        // Tabindex
        if (node.hasAttribute('tabindex') && node.getAttribute('tabindex') !== '-1') return true;

        // Inline event handlers
        if (node.hasAttribute('onclick') || node.hasAttribute('onmousedown') ||
            node.hasAttribute('onmouseup') || node.hasAttribute('ontouchstart') ||
            node.hasAttribute('onpointerdown') || node.hasAttribute('onkeydown')) return true;

        // Google-specific framework attributes (jsaction, jscontroller, jsname, data-ved)
        if (node.hasAttribute('jsaction') || node.hasAttribute('jscontroller') ||
            node.hasAttribute('data-ved') || node.hasAttribute('data-hveid')) return true;

        // Common framework data-* click attributes
        if (node.hasAttribute('data-action') || node.hasAttribute('data-click') ||
            node.hasAttribute('data-onclick') || node.hasAttribute('data-command') ||
            node.hasAttribute('data-toggle') || node.hasAttribute('data-dismiss') ||
            node.hasAttribute('data-target') || node.hasAttribute('data-bs-toggle') ||
            node.hasAttribute('ng-click') || node.hasAttribute('v-on:click') ||
            node.hasAttribute('@click')) return true;

        // ARIA popup/expanded/pressed indicators
        if (node.hasAttribute('aria-haspopup') || node.hasAttribute('aria-pressed') ||
            node.hasAttribute('aria-expanded')) return true;

        // Contenteditable
        if (node.hasAttribute('contenteditable') && node.getAttribute('contenteditable') !== 'false') return true;

        // Cursor pointer detection (catches most clickable divs/spans in modern UIs)
        try {
            const style = window.getComputedStyle(node);
            if (style.cursor === 'pointer') {
                // Only count as interactive if the element has reasonable size
                const r = node.getBoundingClientRect();
                if (r.width > 5 && r.height > 5) return true;
            }
        } catch(e) {}

        return false;
    }

    function bounds(node) {
        const r = node.getBoundingClientRect();
        return {x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height)};
    }

    function visible(rect) {
        return rect.top < window.innerHeight && rect.bottom > 0 && rect.left < window.innerWidth && rect.right > 0;
    }

    function getLabel(node) {
        const tag = node.tagName.toLowerCase();
        // For inputs, use value/placeholder
        if (['input','textarea'].includes(tag)) return (node.value || node.placeholder || '').substring(0,200);
        // aria-label first
        const ariaLabel = node.getAttribute('aria-label');
        if (ariaLabel) return ariaLabel.substring(0,200);
        // title attribute
        const title = node.getAttribute('title');
        if (title) return title.substring(0,200);
        // innerText (but keep it short for interactive elements)
        const txt = (node.innerText || '').trim().replace(/\n/g,' ');
        return txt.substring(0,200);
    }

    function walk(node) {
        if (seen.has(node)) return;
        seen.add(node);

        if (node.nodeType === Node.ELEMENT_NODE) {
            const tag = node.tagName.toLowerCase();
            if (SKIP_TAGS.has(tag)) return;

            try {
                const style = window.getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return;
            } catch(e) { return; }

            const rect = node.getBoundingClientRect();
            // Skip elements completely outside viewport
            if (rect.width === 0 && rect.height === 0) {
                // Still walk children — some invisible wrappers have visible children
                Array.from(node.childNodes).forEach(c => walk(c));
                if (node.shadowRoot) Array.from(node.shadowRoot.childNodes).forEach(c => walk(c));
                return;
            }

            const vis = visible(rect);

            if (vis && isInteractive(node)) {
                const ariaLabel = node.getAttribute('aria-label') || '';
                const ariaExpanded = node.getAttribute('aria-expanded');
                const ariaChecked = node.getAttribute('aria-checked');
                const ariaSelected = node.getAttribute('aria-selected');
                const ariaDisabled = node.getAttribute('aria-disabled') === 'true' || node.hasAttribute('disabled');
                const role = node.getAttribute('role') || '';
                const href = (tag === 'a') ? (node.getAttribute('href') || '') : '';

                const a = [];
                if (ariaLabel) a.push('label:"' + ariaLabel + '"');
                if (ariaExpanded !== null && ariaExpanded !== undefined) a.push('expanded:' + ariaExpanded);
                if (ariaChecked !== null && ariaChecked !== undefined) a.push('checked:' + ariaChecked);
                if (ariaSelected !== null && ariaSelected !== undefined) a.push('selected:' + ariaSelected);
                if (ariaDisabled) a.push('disabled');

                const id = idCounter++;
                node.setAttribute('data-cli-id', id);

                let val = getLabel(node);

                items.push({
                    id: id, tag: tag, text: val,
                    type: node.type || node.getAttribute('type') || '',
                    interactive: true, role: role, aria: a.join(' '),
                    href: href.substring(0,120), bounds: bounds(node)
                });

                if (!['input','textarea','select'].includes(tag)) {
                    Array.from(node.childNodes).forEach(c => walk(c));
                    if (node.shadowRoot) Array.from(node.shadowRoot.childNodes).forEach(c => walk(c));
                }
                return;
            }

            if (vis && tag === 'img') {
                items.push({id:null, tag:'img', text:node.getAttribute('alt')||'', type:'', interactive:false, role:'', aria:'', href:'', bounds:bounds(node)});
                return;
            }
            if (vis && tag === 'svg') {
                const t = node.querySelector('title');
                items.push({id:null, tag:'svg', text:t?t.textContent:(node.getAttribute('aria-label')||'icon'), type:'', interactive:false, role:'', aria:'', href:'', bounds:bounds(node)});
                return;
            }

            Array.from(node.childNodes).forEach(c => walk(c));
            // Traverse shadow DOM
            if (node.shadowRoot) Array.from(node.shadowRoot.childNodes).forEach(c => walk(c));

        } else if (node.nodeType === Node.TEXT_NODE) {
            const t = node.textContent.trim().replace(/\n/g,' ');
            if (t.length > 0) {
                items.push({id:null, tag:'text', text:t, type:'', interactive:false, role:'', aria:'', href:'', bounds:null});
            }
        }
    }

    walk(document.body);

    // Also walk any top-level popover/dialog elements that may live outside body flow
    document.querySelectorAll('dialog[open], [popover]:popover-open, [role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"]').forEach(el => {
        if (!seen.has(el)) walk(el);
    });

    return {
        items: items,
        scroll_y: Math.round(window.scrollY),
        inner_height: window.innerHeight,
        scroll_height: document.body.scrollHeight,
        title: document.title,
        url: window.location.href
    };
})();
"""

# Full-page scan JS (traverses ENTIRE page, not just viewport)
JS_SCAN = r"""
(function() {
    let idCounter = 1;
    const items = [];
    const seen = new WeakSet();

    document.querySelectorAll('[data-cli-id]').forEach(el => el.removeAttribute('data-cli-id'));

    const INTERACTIVE_TAGS = new Set(['a','button','input','textarea','select','details','summary','dialog','menu']);
    const INTERACTIVE_ROLES = new Set([
        'button','link','checkbox','menuitem','menuitemcheckbox','menuitemradio',
        'radio','tab','treeitem','switch','searchbox','textbox','combobox','option',
        'dialog','alertdialog','menu','menubar','listbox','gridcell','row',
        'slider','spinbutton','scrollbar','separator','progressbar',
        'navigation','toolbar','tablist','tabpanel','tooltip','alert',
        'listitem','figure','form','img','log','marquee','math','note',
        'status','timer','banner','complementary','contentinfo','main','region','search'
    ]);
    const SKIP_TAGS = new Set(['script','style','noscript','meta','head','br','hr','link']);

    function isInteractive(node) {
        const tag = node.tagName.toLowerCase();
        if (INTERACTIVE_TAGS.has(tag)) return true;

        // ARIA roles
        const role = (node.getAttribute('role') || '').toLowerCase();
        if (role && INTERACTIVE_ROLES.has(role)) return true;

        // Tabindex
        if (node.hasAttribute('tabindex') && node.getAttribute('tabindex') !== '-1') return true;

        // Inline event handlers
        if (node.hasAttribute('onclick') || node.hasAttribute('onmousedown') ||
            node.hasAttribute('onmouseup') || node.hasAttribute('ontouchstart') ||
            node.hasAttribute('onpointerdown') || node.hasAttribute('onkeydown')) return true;

        // Google-specific framework attributes (jsaction, jscontroller, jsname, data-ved)
        if (node.hasAttribute('jsaction') || node.hasAttribute('jscontroller') ||
            node.hasAttribute('data-ved') || node.hasAttribute('data-hveid')) return true;

        // Common framework data-* click attributes
        if (node.hasAttribute('data-action') || node.hasAttribute('data-click') ||
            node.hasAttribute('data-onclick') || node.hasAttribute('data-command') ||
            node.hasAttribute('data-toggle') || node.hasAttribute('data-dismiss') ||
            node.hasAttribute('data-target') || node.hasAttribute('data-bs-toggle') ||
            node.hasAttribute('ng-click') || node.hasAttribute('v-on:click') ||
            node.hasAttribute('@click')) return true;

        // ARIA popup/expanded/pressed indicators
        if (node.hasAttribute('aria-haspopup') || node.hasAttribute('aria-pressed') ||
            node.hasAttribute('aria-expanded')) return true;

        // Contenteditable
        if (node.hasAttribute('contenteditable') && node.getAttribute('contenteditable') !== 'false') return true;

        // Cursor pointer detection (catches most clickable divs/spans in modern UIs)
        try {
            const style = window.getComputedStyle(node);
            if (style.cursor === 'pointer') {
                const r = node.getBoundingClientRect();
                if (r.width > 5 && r.height > 5) return true;
            }
        } catch(e) {}

        return false;
    }

    function bounds(node) {
        const r = node.getBoundingClientRect();
        return {x: Math.round(r.left), y: Math.round(r.top + window.scrollY), w: Math.round(r.width), h: Math.round(r.height)};
    }

    function getLabel(node) {
        const tag = node.tagName.toLowerCase();
        if (['input','textarea'].includes(tag)) return (node.value || node.placeholder || '').substring(0,200);
        const ariaLabel = node.getAttribute('aria-label');
        if (ariaLabel) return ariaLabel.substring(0,200);
        const title = node.getAttribute('title');
        if (title) return title.substring(0,200);
        const txt = (node.innerText || '').trim().replace(/\n/g,' ');
        return txt.substring(0,200);
    }

    function walk(node) {
        if (seen.has(node)) return;
        seen.add(node);

        if (node.nodeType === Node.ELEMENT_NODE) {
            const tag = node.tagName.toLowerCase();
            if (SKIP_TAGS.has(tag)) return;

            try {
                const style = window.getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return;
            } catch(e) { return; }

            const rect = node.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) {
                Array.from(node.childNodes).forEach(c => walk(c));
                if (node.shadowRoot) Array.from(node.shadowRoot.childNodes).forEach(c => walk(c));
                return;
            }

            if (isInteractive(node)) {
                const ariaLabel = node.getAttribute('aria-label') || '';
                const role = node.getAttribute('role') || '';
                const href = (tag === 'a') ? (node.getAttribute('href') || '') : '';
                const ariaExpanded = node.getAttribute('aria-expanded');
                const ariaChecked = node.getAttribute('aria-checked');
                const ariaSelected = node.getAttribute('aria-selected');
                const ariaDisabled = node.getAttribute('aria-disabled') === 'true' || node.hasAttribute('disabled');

                const a = [];
                if (ariaLabel) a.push('label:"' + ariaLabel + '"');
                if (ariaExpanded !== null && ariaExpanded !== undefined) a.push('expanded:' + ariaExpanded);
                if (ariaChecked !== null && ariaChecked !== undefined) a.push('checked:' + ariaChecked);
                if (ariaSelected !== null && ariaSelected !== undefined) a.push('selected:' + ariaSelected);
                if (ariaDisabled) a.push('disabled');

                const id = idCounter++;
                node.setAttribute('data-cli-id', id);

                let val = getLabel(node);

                items.push({
                    id: id, tag: tag, text: val,
                    type: node.type || node.getAttribute('type') || '',
                    interactive: true, role: role, aria: a.join(' '),
                    href: href.substring(0,120), bounds: bounds(node)
                });

                if (!['input','textarea','select'].includes(tag)) {
                    Array.from(node.childNodes).forEach(c => walk(c));
                    if (node.shadowRoot) Array.from(node.shadowRoot.childNodes).forEach(c => walk(c));
                }
                return;
            }

            if (tag === 'img') {
                items.push({id:null, tag:'img', text:node.getAttribute('alt')||'', type:'', interactive:false, role:'', aria:'', href:'', bounds:bounds(node)});
                return;
            }
            if (tag === 'svg') {
                const t = node.querySelector('title');
                items.push({id:null, tag:'svg', text:t?t.textContent:(node.getAttribute('aria-label')||'icon'), type:'', interactive:false, role:'', aria:'', href:'', bounds:bounds(node)});
                return;
            }

            Array.from(node.childNodes).forEach(c => walk(c));
            if (node.shadowRoot) Array.from(node.shadowRoot.childNodes).forEach(c => walk(c));

        } else if (node.nodeType === Node.TEXT_NODE) {
            const t = node.textContent.trim().replace(/\n/g,' ');
            if (t.length > 0) {
                items.push({id:null, tag:'text', text:t, type:'', interactive:false, role:'', aria:'', href:'', bounds:null});
            }
        }
    }

    walk(document.body);

    // Also walk any top-level popover/dialog elements outside normal body flow
    document.querySelectorAll('dialog[open], [popover]:popover-open, [role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"]').forEach(el => {
        if (!seen.has(el)) walk(el);
    });

    return {
        items: items,
        total_interactive: items.filter(i => i.interactive).length,
        total_text: items.filter(i => !i.interactive).length,
        scroll_height: document.body.scrollHeight,
        title: document.title,
        url: window.location.href
    };
})();
"""

JS_CLEAR_BOXES = """
(() => {
    document.querySelectorAll('.cli-vision-box, .cli-vision-badge').forEach(el => el.remove());
})();
"""


# ─── CLI ─────────────────────────────────────────────────────────────────────


class BrowserCLI(cmd.Cmd):

    # Type hints for linter
    intro: str = (
        '\n\033[1m╔══════════════════════════════════════════════╗\033[0m\n'
        '\033[1m║   Visionless Browser CLI (Real Chrome + CDP)  ║\033[0m\n'
        '\033[1m╚══════════════════════════════════════════════╝\033[0m\n'
        '\n\033[93mAll Playwright MCP features included.\033[0m\n'
        'Type \033[96mhelp\033[0m or \033[96m?\033[0m to list commands.\n'
        'Start: \033[96mgo google.com\033[0m\n'
    )
    prompt: str = '\n\033[95mbrowser>\033[0m '
    
    latest_frame: bytes | None
    cdp_session: Any
    pw: Any
    browser: Any
    context: Any
    page: Any
    chrome_process: subprocess.Popen
    image_dir: str
    screenshot_counter: int
    console_messages: list
    network_requests: list
    chaining: bool
    frame_condition: Any

    def __init__(self):
        super().__init__()
        import threading
        from typing import Any
        self.frame_condition = threading.Condition()
        self.latest_frame: bytes | None = None
        
        self.screenshot_counter = 0
        self.image_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_images")
        os.makedirs(self.image_dir, exist_ok=True)
        self.pw = sync_playwright().start()
        self.console_messages = []
        self.network_requests = []
        self._chaining = False  # Flag to skip intermediate sleep/show during chain

        print("Launching Chrome (headless)...")
        self.chrome_process = subprocess.Popen(
            '/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --headless=new --remote-debugging-port=9222 --user-data-dir=~/chrome-debug',
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        time.sleep(2)  # Give Chrome time to open the debug port

        print("Connecting to Chrome via CDP on port 9222...")
        try:
            self.browser = self.pw.chromium.connect_over_cdp("http://localhost:9222")
        except Exception as e:
            print(f"\033[91mFailed to connect: {e}\033[0m")
            print("\nEnsure Chrome is successfully launching with:")
            print('  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222 --user-data-dir=~/chrome-debug')
            self.pw.stop()
            sys.exit(1)

        if self.browser.contexts:
            self.context = self.browser.contexts[0]
            valid_page = self._get_valid_page()
            if valid_page:
                self.page = valid_page
            else:
                self.page = self.context.new_page()
        else:
            self.context = self.browser.new_context()
            self.page = self.context.new_page()

        self._attach_listeners()
        print(f"\033[92m✓ Connected! Page: {self.page.title()} ({self.page.url})\033[0m")
        
        print(f"\n[Vision Mode] Navigating quietly to google.com...")
        self.do_go("google.com")

    def _get_valid_page(self):
        """Returns the most recent valid webpage, ignoring invisible Chrome system/extension tabs."""
        if not hasattr(self, 'context') or not self.context.pages:
            return None
            
        for p in reversed(self.context.pages):
            url = p.url
            if not url.startswith("chrome://") and not url.startswith("chrome-extension://"):
                return p
        return None

    def _attach_listeners(self):
        """Attach console, network, and live screencast listeners to current page."""
        try:
            self.page.on("console", lambda msg: self.console_messages.append({
                "level": msg.type, "text": msg.text,
                "time": datetime.datetime.now().strftime("%H:%M:%S")
            }))
            self.page.on("request", lambda req: self.network_requests.append({
                "method": req.method, "url": req.url,
                "resource": req.resource_type,
                "time": datetime.datetime.now().strftime("%H:%M:%S")
            }))
        except Exception:
            pass

        # Live Screencasting via CDP
        try:
            self.cdp_session = self.page.context.new_cdp_session(self.page)
            
            def handle_screencast_frame(event):
                try:
                    import base64
                    frame_data = base64.b64decode(event.get('data', ''))
                    
                    with self.frame_condition:
                        self.latest_frame = frame_data
                        self.frame_condition.notify_all()
                    
                    frame_path = os.path.join(self.image_dir, "live_frame.jpg")
                    with open(frame_path, "wb") as f:
                        f.write(frame_data)
                    # Tell Chrome we got the frame so it sends the next one
                    self.cdp_session.send("Page.screencastFrameAck", {"sessionId": event.get("sessionId")})
                except Exception as e:
                    print(f"Screencast frame error: {e}")

            self.cdp_session.on("Page.screencastFrame", handle_screencast_frame)
            self.cdp_session.send("Page.startScreencast", {
                "format": "jpeg",
                "quality": 60,
                "everyNthFrame": 1
            })
        except Exception as e:
            print(f"Failed to start CDP screencast: {e}")

    # ── Helpers ───────────────────────────────────────────────────────────

    def _snapshot(self):
        try:
            return self.page.evaluate(JS_SNAPSHOT)
        except Exception as e:
            print(f"Error getting snapshot: {e}")
            return None

    def _print_state(self, state):
        print("\n" + "=" * 80)
        print(f"Page Title: {state['title']}")
        print(f"URL:        {state['url']}")
        scroll_info = state.get('scroll_y', 0)
        inner_h = state.get('inner_height', 0)
        scroll_h = state.get('scroll_height', 0)
        print(f"Scroll:     {scroll_info}px / {scroll_h}px  (viewport: {inner_h}px)")
        print("-" * 80)
        print("=" * 80)

    def _refresh_page_ref(self):
        valid_page = self._get_valid_page()
        if valid_page:
            self.page = valid_page
            self._attach_listeners()

    def _resolve_element(self, eid):
        selector = f'[data-cli-id="{eid}"]'
        loc = self.page.locator(selector)
        if loc.count() == 0:
            print(f"Element {eid} not found. Use `show` to refresh.")
            return None
        return loc.first

    # ════════════════════════════════════════════════════════════════════════
    #  NAVIGATION
    # ════════════════════════════════════════════════════════════════════════

    def do_go(self, arg):
        '''go <url>  —  Navigate to a URL'''
        if not arg:
            print("Usage: go <url>"); return
        if not arg.startswith("http"):
            arg = "https://" + arg
        print(f"Navigating to {arg}...")
        self.network_requests.clear()
        self.console_messages.clear()
        try:
            self.page.goto(arg, wait_until="domcontentloaded", timeout=15000)
            time.sleep(1)
        except Exception as e:
            print(f"Navigation error: {e}")
        self.do_show("")

    def do_back(self, arg):
        '''back  —  Go back in history'''
        try:
            self.page.go_back(wait_until="domcontentloaded")
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(1)
        self.do_show("")

    def do_forward(self, arg):
        '''forward  —  Go forward in history'''
        try:
            self.page.go_forward(wait_until="domcontentloaded")
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(1)
        self.do_show("")

    def do_reload(self, arg):
        '''reload  —  Reload the current page'''
        self.network_requests.clear()
        self.console_messages.clear()
        self.page.reload(wait_until="domcontentloaded")
        time.sleep(1)
        self.do_show("")

    # ════════════════════════════════════════════════════════════════════════
    #  SNAPSHOT / SHOW / SCAN
    # ════════════════════════════════════════════════════════════════════════

    def do_show(self, arg):
        '''show  —  Display all elements in the current viewport (accessibility snapshot)'''
        state = self._snapshot()
        if state is None:
            print("Could not read page."); return
        self._print_state(state)

    def do_snapshot(self, arg):
        '''snapshot [filename]  —  Playwright's accessibility snapshot, optionally saved to file'''
        try:
            snap = self.page.accessibility.snapshot()
            text = json.dumps(snap, indent=2)
            if arg.strip():
                with open(arg.strip(), "w") as f:
                    f.write(text)
                print(f"Snapshot saved to {arg.strip()}")
            else:
                print(text[:5000])
                if len(text) > 5000:
                    print(f"\n... truncated ({len(text)} chars total)")
        except Exception as e:
            print(f"Snapshot error: {e}")

    def do_scan(self, arg):
        '''scan  —  Full-page scan of ALL elements (not just viewport). Scrolls through entire page.'''
        print("Scanning entire page...")
        try:
            state = self.page.evaluate(JS_SCAN)
        except Exception as e:
            print(f"Scan error: {e}"); return

        print("\n" + "=" * 80)
        print(f"[FULL PAGE SCAN]")
        print(f"Page Title: {state['title']}")
        print(f"URL:        {state['url']}")
        print(f"Page Height: {state['scroll_height']}px")
        print(f"Interactive: {state['total_interactive']} elements")
        print(f"Text nodes:  {state['total_text']}")
        print("-" * 80)

        for item in state["items"]:
            if item["interactive"]:
                parts = [f"[{item['id']}] <{item['tag']}>"]
                if item["type"]: parts.append(f"[type={item['type']}]")
                if item["role"]: parts.append(f"[role={item['role']}]")
                if item["aria"]: parts.append(f"[aria: {item['aria']}]")
                if item["href"]: parts.append(f"[href={item['href']}]")
                if item["text"]: parts.append(f"'{item['text']}'")
                print(" ".join(parts))
            else:
                if item["tag"] == "text":
                    print(f'    "{item["text"]}"')
                else:
                    print(f"    <{item['tag']}> '{item['text']}'")

        print("=" * 80)

    def do_vision(self, arg):
        '''vision [filename]  —  Draw bounding boxes over elements, take screenshot, and output clean elements'''
        filename = arg.strip() if arg.strip() else "vision.png"
        if not os.path.isabs(filename):
            filename = os.path.join(self.image_dir, filename)
        print(f"Preparing vision bounding boxes for {filename}...")
        
        try:
            # 1. First run scan to assign `data-cli-id` to everything and extract physical geometric bounds
            state = self.page.evaluate(JS_SCAN)
            
            # 2. Draw boxes based PRECISELY on those extracted coordinate bounds, not by re-evaluating DOM elements
            boxes_js = []
            for item in state.get("items", []):
                if item.get("interactive") and item.get("bounds") and item["bounds"]["w"] > 0 and item["bounds"]["h"] > 0:
                    b = item["bounds"]
                    eid = item["id"]
                    boxes_js.append(f"""
                        var box = document.createElement('div');
                        box.className = 'cli-vision-box';
                        box.style.position = 'absolute';
                        box.style.left = '{b['x']}px';
                        box.style.top = '{b['y']}px';
                        box.style.width = '{b['w']}px';
                        box.style.height = '{b['h']}px';
                        box.style.border = '2px dashed red';
                        box.style.boxSizing = 'border-box';
                        box.style.pointerEvents = 'none';
                        box.style.zIndex = '2147483646';
                        
                        var badge = document.createElement('div');
                        badge.className = 'cli-vision-badge';
                        badge.textContent = '{eid}';
                        badge.style.position = 'absolute';
                        badge.style.left = '{(b['x'] - 2)}px';
                        badge.style.top = '{(b['y'] - 16)}px';
                        badge.style.backgroundColor = 'red';
                        badge.style.color = 'white';
                        badge.style.fontSize = '12px';
                        badge.style.fontWeight = 'bold';
                        badge.style.padding = '1px 4px';
                        badge.style.border = '1px solid darkred';
                        badge.style.borderRadius = '3px';
                        badge.style.pointerEvents = 'none';
                        badge.style.zIndex = '2147483647';
                        
                        document.body.appendChild(box);
                        document.body.appendChild(badge);
                    """)
                    
            draw_script = "(() => {\n" + "\n".join(boxes_js) + "\n})();"
            self.page.evaluate(draw_script)
            
            # 3. Take screenshot (viewport only, as requested)
            self.page.screenshot(path=filename, full_page=False)
            print(f"Screenshot saved to {filename} (viewport only)")
            
            # 4. Clean up boxes
            self.page.evaluate(JS_CLEAR_BOXES)
            
        except Exception as e:
            print(f"Vision error: {e}"); return
            
        # 5. Output the clean format text for AI context
        print("\n" + "=" * 80)
        print(f"[VISION SCAN RESULTS]")
        print(f"Page Title: {state.get('title', '')}")
        print(f"URL:        {state.get('url', '')}")
        print(f"Page Height: {state.get('scroll_height', 0)}px")
        print(f"Interactive: {state.get('total_interactive', 0)} elements")
        print(f"Text nodes:  {state.get('total_text', 0)}")
        print("-" * 80)
        print("=" * 80)

    # ════════════════════════════════════════════════════════════════════════
    #  CLICK / HOVER / DOUBLE-CLICK
    # ════════════════════════════════════════════════════════════════════════

    def do_click(self, arg):
        '''click <id> [right|middle]  —  Click an element (optionally right/middle click)'''
        parts = arg.strip().split()
        if not parts:
            print("Usage: click <id> [right|middle]"); return
        try:
            eid = int(parts[0])
        except ValueError:
            print("ID must be numeric."); return

        button = parts[1] if len(parts) > 1 else "left"
        el = self._resolve_element(eid)
        if not el: return

        print(f"Clicking element {eid} ({button})...")
        try:
            el.click(button=button, timeout=5000)
        except Exception as e:
            print(f"Click error: {e}"); return
        if not self._chaining:
            time.sleep(1.5)
            self._refresh_page_ref()
            self.do_show("")
        else:
            time.sleep(0.3)

    def do_dblclick(self, arg):
        '''dblclick <id>  —  Double-click an element'''
        try:
            eid = int(arg.strip())
        except (ValueError, AttributeError):
            print("Usage: dblclick <id>"); return
        el = self._resolve_element(eid)
        if not el: return
        print(f"Double-clicking element {eid}...")
        try:
            el.dblclick(timeout=5000)
        except Exception as e:
            print(f"Error: {e}"); return
        time.sleep(1)
        self.do_show("")

    def do_hover(self, arg):
        '''hover <id>  —  Hover over an element'''
        try:
            eid = int(arg.strip())
        except (ValueError, AttributeError):
            print("Usage: hover <id>"); return
        el = self._resolve_element(eid)
        if not el: return
        print(f"Hovering over element {eid}...")
        try:
            el.hover(timeout=5000)
        except Exception as e:
            print(f"Error: {e}"); return
        time.sleep(0.5)
        self.do_show("")

    # ════════════════════════════════════════════════════════════════════════
    #  TYPE / FILL / SELECT / PRESS KEY
    # ════════════════════════════════════════════════════════════════════════

    def do_type(self, arg):
        '''type <id> <text>  —  Type text into a field (fills at once)'''
        parts = arg.split(" ", 1)
        if len(parts) < 2:
            print("Usage: type <id> <text>"); return
        try:
            eid = int(parts[0])
        except ValueError:
            print("ID must be numeric."); return
        el = self._resolve_element(eid)
        if not el: return
        print(f"Typing into element {eid}...")
        try:
            el.fill(parts[1])
        except Exception as e:
            print(f"Type error: {e}"); return
        if not self._chaining:
            time.sleep(0.5)
            self.do_show("")
        else:
            time.sleep(0.2)

    def do_typeslow(self, arg):
        '''typeslow <id> <text>  —  Type text char-by-char (triggers key handlers)'''
        parts = arg.split(" ", 1)
        if len(parts) < 2:
            print("Usage: typeslow <id> <text>"); return
        try:
            eid = int(parts[0])
        except ValueError:
            print("ID must be numeric."); return
        el = self._resolve_element(eid)
        if not el: return
        print(f"Typing slowly into element {eid}...")
        try:
            el.click(timeout=3000)
            self.page.keyboard.type(parts[1], delay=50)
        except Exception as e:
            print(f"Error: {e}"); return
        time.sleep(0.5)
        self.do_show("")

    def do_submit(self, arg):
        '''submit <id>  —  Press Enter on a field'''
        try:
            eid = int(arg.strip())
        except (ValueError, AttributeError):
            print("Usage: submit <id>"); return
        el = self._resolve_element(eid)
        if not el: return
        print(f"Pressing Enter on element {eid}...")
        try:
            el.press("Enter")
        except Exception as e:
            print(f"Error: {e}"); return
        if not self._chaining:
            time.sleep(1.5)
            self._refresh_page_ref()
            self.do_show("")
        else:
            time.sleep(0.3)

    def do_chain(self, arg):
        '''chain <click|type|submit|scroll|press|go|hover> ...  —  Run multiple browser actions in one call.
        Example: chain click 9 type 9 OpenAI submit 9
        Splits on command keywords and runs them sequentially.'''
        if not arg.strip():
            print("Usage: chain click 9 type 9 hello world submit 9"); return

        # Keywords that start a new sub-command
        CMD_KEYWORDS = {'click', 'type', 'typeslow', 'submit', 'scroll', 'press', 'go',
                        'hover', 'select', 'clear', 'check', 'uncheck', 'dblclick',
                        'focus', 'back', 'forward', 'reload', 'wait'}
        
        # Split the compound string into individual commands
        tokens = arg.strip().split()
        commands = []
        current_cmd = None
        current_args = []
        
        for token in tokens:
            if token.lower() in CMD_KEYWORDS:
                # Save previous command if any
                if current_cmd:
                    commands.append((current_cmd, " ".join(current_args)))
                current_cmd = token.lower()
                current_args = []
            else:
                current_args.append(token)
        
        # Save last command
        if current_cmd:
            commands.append((current_cmd, " ".join(current_args)))
        
        if not commands:
            print("No valid commands found. Usage: chain click 9 type 9 hello submit 9"); return
        
        print(f"Running {len(commands)} chained actions...")
        
        # Set chaining flag to skip intermediate sleep/show in sub-commands
        self._chaining = True
        try:
            for i, (cmd_name, cmd_arg) in enumerate(commands):
                print(f"\n  [{i+1}/{len(commands)}] {cmd_name} {cmd_arg}")
                method = getattr(self, f"do_{cmd_name}", None)
                if method:
                    try:
                        method(cmd_arg)
                    except Exception as e:
                        print(f"  Error in {cmd_name}: {e}")
                        break
                else:
                    print(f"  Unknown command: {cmd_name}")
                    break
        finally:
            self._chaining = False
        
        # Final refresh + show after all actions complete
        time.sleep(1)
        self._refresh_page_ref()
        self.do_show("")
        print(f"\nChain complete ({len(commands)} actions executed).")

    def do_select(self, arg):
        '''select <id> <value> [value2 ...]  —  Select option(s) in a dropdown'''
        parts = arg.split()
        if len(parts) < 2:
            print("Usage: select <id> <value> [value2 ...]"); return
        try:
            eid = int(parts[0])
        except ValueError:
            print("ID must be numeric."); return
        values = parts[1:]
        el = self._resolve_element(eid)
        if not el: return
        print(f"Selecting {values} in element {eid}...")
        try:
            el.select_option(values)
        except Exception as e:
            print(f"Error: {e}"); return
        time.sleep(0.5)
        self.do_show("")

    def do_press(self, arg):
        '''press <key>  —  Press a keyboard key (e.g. Enter, Tab, ArrowDown, Escape, a, Control+c)'''
        if not arg.strip():
            print("Usage: press <key>"); return
        print(f"Pressing key: {arg.strip()}")
        try:
            self.page.keyboard.press(arg.strip())
        except Exception as e:
            print(f"Error: {e}"); return
        time.sleep(0.5)
        self.do_show("")

    def do_focus(self, arg):
        '''focus <id>  —  Focus an element'''
        try:
            eid = int(arg.strip())
        except (ValueError, AttributeError):
            print("Usage: focus <id>"); return
        el = self._resolve_element(eid)
        if not el: return
        try:
            el.focus()
        except Exception as e:
            print(f"Error: {e}"); return
        print(f"Focused element {eid}.")

    def do_clear(self, arg):
        '''clear <id>  —  Clear an input field'''
        try:
            eid = int(arg.strip())
        except (ValueError, AttributeError):
            print("Usage: clear <id>"); return
        el = self._resolve_element(eid)
        if not el: return
        try:
            el.fill("")
        except Exception as e:
            print(f"Error: {e}"); return
        print(f"Cleared element {eid}.")

    def do_check(self, arg):
        '''check <id>  —  Check a checkbox/radio'''
        try:
            eid = int(arg.strip())
        except (ValueError, AttributeError):
            print("Usage: check <id>"); return
        el = self._resolve_element(eid)
        if not el: return
        try:
            el.check()
        except Exception as e:
            print(f"Error: {e}"); return
        print(f"Checked element {eid}.")

    def do_uncheck(self, arg):
        '''uncheck <id>  —  Uncheck a checkbox'''
        try:
            eid = int(arg.strip())
        except (ValueError, AttributeError):
            print("Usage: uncheck <id>"); return
        el = self._resolve_element(eid)
        if not el: return
        try:
            el.uncheck()
        except Exception as e:
            print(f"Error: {e}"); return
        print(f"Unchecked element {eid}.")

    # ════════════════════════════════════════════════════════════════════════
    #  MOUSE (xy-based operations)
    # ════════════════════════════════════════════════════════════════════════

    def do_clickxy(self, arg):
        '''clickxy <x> <y>  —  Click at exact pixel coordinates'''
        parts = arg.split()
        if len(parts) != 2:
            print("Usage: clickxy <x> <y>"); return
        try:
            x, y = float(parts[0]), float(parts[1])
        except ValueError:
            print("Coordinates must be numbers."); return
        print(f"Clicking at ({x}, {y})...")
        self.page.mouse.click(x, y)
        time.sleep(1)
        self.do_show("")

    def do_movexy(self, arg):
        '''movexy <x> <y>  —  Move mouse to coordinates'''
        parts = arg.split()
        if len(parts) != 2:
            print("Usage: movexy <x> <y>"); return
        try:
            x, y = float(parts[0]), float(parts[1])
        except ValueError:
            print("Coordinates must be numbers."); return
        self.page.mouse.move(x, y)
        print(f"Mouse moved to ({x}, {y}).")

    def do_dragxy(self, arg):
        '''dragxy <x1> <y1> <x2> <y2>  —  Drag from one point to another'''
        parts = arg.split()
        if len(parts) != 4:
            print("Usage: dragxy <x1> <y1> <x2> <y2>"); return
        try:
            x1, y1, x2, y2 = [float(p) for p in parts]
        except ValueError:
            print("Coordinates must be numbers."); return
        print(f"Dragging from ({x1},{y1}) to ({x2},{y2})...")
        self.page.mouse.move(x1, y1)
        self.page.mouse.down()
        self.page.mouse.move(x2, y2, steps=10)
        self.page.mouse.up()
        time.sleep(0.5)
        self.do_show("")

    def do_drag(self, arg):
        '''drag <id_from> <id_to>  —  Drag and drop between two elements'''
        parts = arg.split()
        if len(parts) != 2:
            print("Usage: drag <id_from> <id_to>"); return
        try:
            eid1, eid2 = int(parts[0]), int(parts[1])
        except ValueError:
            print("IDs must be numeric."); return
        src = self._resolve_element(eid1)
        dst = self._resolve_element(eid2)
        if not src or not dst: return
        print(f"Dragging element {eid1} to element {eid2}...")
        try:
            src.drag_to(dst)
        except Exception as e:
            print(f"Error: {e}"); return
        time.sleep(0.5)
        self.do_show("")

    def do_mousedown(self, arg):
        '''mousedown [left|right|middle]  —  Press mouse button down'''
        button = arg.strip() if arg.strip() else "left"
        self.page.mouse.down(button=button)
        print(f"Mouse {button} button down.")

    def do_mouseup(self, arg):
        '''mouseup [left|right|middle]  —  Release mouse button'''
        button = arg.strip() if arg.strip() else "left"
        self.page.mouse.up(button=button)
        print(f"Mouse {button} button up.")

    def do_wheel(self, arg):
        '''wheel <deltaX> <deltaY>  —  Scroll mouse wheel by exact pixel amounts'''
        parts = arg.split()
        if len(parts) != 2:
            print("Usage: wheel <deltaX> <deltaY>"); return
        try:
            dx, dy = float(parts[0]), float(parts[1])
        except ValueError:
            print("Deltas must be numbers."); return
        self.page.mouse.wheel(dx, dy)
        time.sleep(0.5)
        self.do_show("")

    # ════════════════════════════════════════════════════════════════════════
    #  SCROLL
    # ════════════════════════════════════════════════════════════════════════

    def do_scroll(self, arg):
        '''scroll <down|up|top|bottom>  —  Scroll the viewport'''
        direction = arg.strip().lower()
        if direction == "up":
            self.page.evaluate("window.scrollBy(0, -window.innerHeight * 0.8)")
        elif direction == "top":
            self.page.evaluate("window.scrollTo(0, 0)")
        elif direction == "bottom":
            self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        else:
            self.page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
        time.sleep(0.5)
        self.do_show("")

    # ════════════════════════════════════════════════════════════════════════
    #  EVALUATE JS
    # ════════════════════════════════════════════════════════════════════════

    def do_eval(self, arg):
        '''eval <javascript>  —  Evaluate JavaScript on the page'''
        if not arg.strip():
            print("Usage: eval <javascript expression>"); return
        try:
            result = self.page.evaluate(arg.strip())
            if result is not None:
                if isinstance(result, (dict, list)):
                    print(json.dumps(result, indent=2)[:3000])
                else:
                    print(result)
            else:
                print("(undefined)")
        except Exception as e:
            print(f"Eval error: {e}")

    # ════════════════════════════════════════════════════════════════════════
    #  SCREENSHOTS / PDF
    # ════════════════════════════════════════════════════════════════════════

    def do_screenshot(self, arg):
        '''screenshot [filename] [--full]  —  Take a screenshot (default: viewport, --full for full page)'''
        parts = arg.strip().split()
        full_page = "--full" in parts
        parts = [p for p in parts if p != "--full"]
        filename = parts[0] if parts else f"page-{int(time.time())}.png"
        if not os.path.isabs(filename):
            filename = os.path.join(self.image_dir, filename)

        print(f"Taking {'full-page ' if full_page else ''}screenshot...")
        try:
            self.page.screenshot(path=filename, full_page=full_page)
            abs_path = os.path.abspath(filename)
            print(f"\033[92mScreenshot saved: {abs_path}\033[0m")
        except Exception as e:
            print(f"Error: {e}")

    def do_pdf(self, arg):
        '''pdf [filename]  —  Save page as PDF'''
        filename = arg.strip() if arg.strip() else f"page-{int(time.time())}.pdf"
        print(f"Saving page as PDF...")
        try:
            self.page.pdf(path=filename)
            print(f"\033[92mPDF saved: {os.path.abspath(filename)}\033[0m")
        except Exception as e:
            print(f"Error: {e}")

    # ════════════════════════════════════════════════════════════════════════
    #  FILE UPLOAD
    # ════════════════════════════════════════════════════════════════════════

    def do_upload(self, arg):
        '''upload <id> <filepath> [filepath2 ...]  —  Upload file(s) to an input'''
        parts = arg.split()
        if len(parts) < 2:
            print("Usage: upload <id> <filepath> [filepath2 ...]"); return
        try:
            eid = int(parts[0])
        except ValueError:
            print("ID must be numeric."); return
        files = parts[1:]
        el = self._resolve_element(eid)
        if not el: return
        print(f"Uploading {files} to element {eid}...")
        try:
            el.set_input_files(files)
        except Exception as e:
            print(f"Error: {e}"); return
        print("Upload complete.")

    # ════════════════════════════════════════════════════════════════════════
    #  DIALOG HANDLING
    # ════════════════════════════════════════════════════════════════════════

    def do_dialog(self, arg):
        '''dialog <accept|dismiss> [prompt_text]  —  Handle the next dialog (alert/confirm/prompt)'''
        parts = arg.strip().split(" ", 1)
        action = parts[0].lower() if parts else ""
        prompt_text = parts[1] if len(parts) > 1 else None

        if action not in ("accept", "dismiss"):
            print("Usage: dialog <accept|dismiss> [prompt_text]"); return

        def handle(dialog):
            if action == "accept":
                if prompt_text:
                    dialog.accept(prompt_text)
                else:
                    dialog.accept()
            else:
                dialog.dismiss()
            print(f"Dialog {action}ed: {dialog.message}")

        self.page.once("dialog", handle)
        print(f"Waiting for next dialog (will {action})...")

    # ════════════════════════════════════════════════════════════════════════
    #  CONSOLE / NETWORK
    # ════════════════════════════════════════════════════════════════════════

    def do_console(self, arg):
        '''console [error|warning|info|all]  —  Show console messages'''
        level = arg.strip().lower() if arg.strip() else "all"
        severity = {"error": ["error"], "warning": ["error", "warning"],
                     "info": ["error", "warning", "info", "log"], "all": None}
        allowed = severity.get(level, None)

        msgs = self.console_messages
        if allowed:
            msgs = [m for m in msgs if m["level"] in allowed]

        if not msgs:
            print("No console messages captured."); return

        print(f"\n{'='*60}")
        print(f" Console Messages ({len(msgs)})")
        print(f"{'─'*60}")
        for m in msgs[-50:]:
            color = "\033[91m" if m["level"] == "error" else "\033[93m" if m["level"] == "warning" else "\033[0m"
            print(f"  {color}[{m['time']}] [{m['level']}] {m['text'][:200]}\033[0m")
        print(f"{'='*60}")

    def do_network(self, arg):
        '''network [--all]  —  Show network requests (--all includes static resources)'''
        include_static = "--all" in arg
        reqs = self.network_requests
        if not include_static:
            static_types = {"image", "stylesheet", "font", "media", "manifest", "other"}
            reqs = [r for r in reqs if r["resource"] not in static_types]

        if not reqs:
            print("No network requests captured."); return

        print(f"\n{'='*70}")
        print(f" Network Requests ({len(reqs)})")
        print(f"{'─'*70}")
        for r in reqs[-50:]:
            print(f"  [{r['time']}] {r['method']:6s} [{r['resource']:10s}] {r['url'][:100]}")
        print(f"{'='*70}")

    # ════════════════════════════════════════════════════════════════════════
    #  WAIT
    # ════════════════════════════════════════════════════════════════════════

    def do_wait(self, arg):
        '''wait <seconds|text "..."|gone "...">  —  Wait for time, text to appear, or text to disappear'''
        parts = arg.strip().split(" ", 1)
        if not parts:
            print('Usage: wait <seconds> | wait text "..." | wait gone "..."'); return

        if parts[0].replace(".", "").isdigit():
            secs = float(parts[0])
            print(f"Waiting {secs}s...")
            time.sleep(secs)
            self.do_show("")
        elif parts[0] == "text" and len(parts) > 1:
            txt = parts[1].strip('"').strip("'")
            print(f'Waiting for text: "{txt}"...')
            try:
                self.page.wait_for_selector(f"text={txt}", timeout=15000)
                print("Text appeared!")
            except Exception:
                print("Timed out waiting for text.")
            self.do_show("")
        elif parts[0] == "gone" and len(parts) > 1:
            txt = parts[1].strip('"').strip("'")
            print(f'Waiting for text to disappear: "{txt}"...')
            try:
                self.page.wait_for_selector(f"text={txt}", state="hidden", timeout=15000)
                print("Text gone!")
            except Exception:
                print("Timed out.")
            self.do_show("")
        else:
            print('Usage: wait <seconds> | wait text "..." | wait gone "..."')

    # ════════════════════════════════════════════════════════════════════════
    #  TABS
    # ════════════════════════════════════════════════════════════════════════

    def do_tabs(self, arg):
        '''tabs  —  List all open tabs'''
        pages = self.context.pages
        print(f"\n{'='*60}")
        print(f" Open Tabs ({len(pages)})")
        print(f"{'─'*60}")
        for i, pg in enumerate(pages):
            current = " \033[92m◀ current\033[0m" if pg == self.page else ""
            title = "(loading)"
            try: title = pg.title()[:50]
            except Exception: pass
            print(f"  [{i}] {title}{current}")
            print(f"      {pg.url[:60]}")
        print(f"{'='*60}")

    def do_tab(self, arg):
        '''tab <n>  —  Switch to tab n'''
        try:
            idx = int(arg.strip())
        except (ValueError, AttributeError):
            print("Usage: tab <n>"); return
        pages = self.context.pages
        if idx < 0 or idx >= len(pages):
            print(f"Tab {idx} doesn't exist."); return
        self.page = pages[idx]
        self._attach_listeners()
        try: self.page.bring_to_front()
        except Exception: pass
        print(f"Switched to tab [{idx}]")
        time.sleep(0.5)
        self.do_show("")

    def do_newtab(self, arg):
        '''newtab [url]  —  Open a new tab'''
        url = arg.strip() if arg.strip() else "about:blank"
        if not url.startswith("http") and url != "about:blank":
            url = "https://" + url
        self.page = self.context.new_page()
        self._attach_listeners()
        if url != "about:blank":
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
            except Exception as e:
                print(f"Navigation error: {e}")
        time.sleep(1)
        print(f"Opened new tab: {url}")
        self.do_show("")

    def do_closetab(self, arg):
        '''closetab [n]  —  Close tab n (or current tab if no n given)'''
        pages = self.context.pages
        if arg.strip():
            try:
                idx = int(arg.strip())
            except ValueError:
                print("Usage: closetab [n]"); return
            if idx < 0 or idx >= len(pages):
                print(f"Tab {idx} doesn't exist."); return
            pages[idx].close()
            print(f"Closed tab [{idx}].")
        else:
            self.page.close()
            print("Closed current tab.")

        if self.context.pages:
            self.page = self.context.pages[-1]
            self._attach_listeners()
            self.do_show("")
        else:
            print("No tabs remaining.")

    # ════════════════════════════════════════════════════════════════════════
    #  RESIZE
    # ════════════════════════════════════════════════════════════════════════

    def do_resize(self, arg):
        '''resize <width> <height>  —  Resize the browser viewport'''
        parts = arg.split()
        if len(parts) != 2:
            print("Usage: resize <width> <height>"); return
        try:
            w, h = int(parts[0]), int(parts[1])
        except ValueError:
            print("Width and height must be integers."); return
        try:
            self.page.set_viewport_size({"width": w, "height": h})
            print(f"Viewport resized to {w}x{h}.")
        except Exception as e:
            print(f"Error: {e}")

    # ════════════════════════════════════════════════════════════════════════
    #  VERIFY
    # ════════════════════════════════════════════════════════════════════════

    def do_visible(self, arg):
        '''visible <text>  —  Verify text is visible on page'''
        if not arg.strip():
            print("Usage: visible <text>"); return
        try:
            loc = self.page.locator(f"text={arg.strip()}")
            if loc.count() > 0 and loc.first.is_visible():
                print(f'\033[92m✓ Text "{arg.strip()}" is visible.\033[0m')
            else:
                print(f'\033[91m✗ Text "{arg.strip()}" is NOT visible.\033[0m')
        except Exception as e:
            print(f"Error: {e}")

    def do_value(self, arg):
        '''value <id>  —  Get the current value of an input element'''
        try:
            eid = int(arg.strip())
        except (ValueError, AttributeError):
            print("Usage: value <id>"); return
        el = self._resolve_element(eid)
        if not el: return
        try:
            val = el.input_value()
            print(f"Value of element {eid}: '{val}'")
        except Exception as e:
            print(f"Error: {e}")

    # ════════════════════════════════════════════════════════════════════════
    #  INFO / SOURCE / URL
    # ════════════════════════════════════════════════════════════════════════

    def do_source(self, arg):
        '''source [n]  —  Show raw HTML (first n chars, default 3000)'''
        limit = 3000
        if arg.strip():
            try: limit = int(arg.strip())
            except ValueError: pass
        try:
            html = self.page.content()
            print(html[:limit])
            if len(html) > limit:
                print(f"\n... ({len(html)} total chars, showing first {limit})")
        except Exception as e:
            print(f"Error: {e}")

    def do_url(self, arg):
        '''url  —  Print the current URL'''
        print(f"Current URL: {self.page.url}")

    def do_title(self, arg):
        '''title  —  Print the current page title'''
        print(f"Title: {self.page.title()}")

    def do_cookies(self, arg):
        '''cookies  —  Show cookies for the current page'''
        try:
            cookies = self.context.cookies()
            domain_cookies = [c for c in cookies if c.get("domain", "") in self.page.url]
            target = domain_cookies if domain_cookies else cookies[:20]
            for c in target:
                print(f"  {c['name']}: {str(c['value'])[:60]}")
            if len(cookies) > len(target):
                print(f"  ... ({len(cookies)} total cookies)")
        except Exception as e:
            print(f"Error: {e}")

    def do_locator(self, arg):
        '''locator <id>  —  Generate a Playwright locator for an element (for test code)'''
        try:
            eid = int(arg.strip())
        except (ValueError, AttributeError):
            print("Usage: locator <id>"); return
        # Read the element's properties and suggest locators
        js = f"""
        (function() {{
            const el = document.querySelector('[data-cli-id="{eid}"]');
            if (!el) return null;
            const tag = el.tagName.toLowerCase();
            const role = el.getAttribute('role') || '';
            const ariaLabel = el.getAttribute('aria-label') || '';
            const text = el.innerText ? el.innerText.trim().substring(0, 50) : '';
            const name = el.getAttribute('name') || '';
            const id = el.id || '';
            const placeholder = el.getAttribute('placeholder') || '';
            return {{tag, role, ariaLabel, text, name, id, placeholder}};
        }})();
        """
        try:
            info = self.page.evaluate(js)
            if not info:
                print(f"Element {eid} not found."); return
            print(f"\n  Suggested Playwright locators for element {eid}:")
            if info["ariaLabel"]:
                print(f'    page.get_by_role("{info["role"] or info["tag"]}", name="{info["ariaLabel"]}")')
            if info["text"]:
                print(f'    page.get_by_text("{info["text"]}")')
            if info["placeholder"]:
                print(f'    page.get_by_placeholder("{info["placeholder"]}")')
            if info["id"]:
                print(f'    page.locator("#{info["id"]}")')
            if info["name"]:
                print(f'    page.locator("[name=\\"{info["name"]}\\"]")')
        except Exception as e:
            print(f"Error: {e}")

    def do_text(self, arg):
        '''text <id>  —  Get the text content of an element'''
        try:
            eid = int(arg.strip())
        except (ValueError, AttributeError):
            print("Usage: text <id>"); return
        el = self._resolve_element(eid)
        if not el: return
        try:
            txt = el.inner_text()
            print(f"Text content:\n{txt[:2000]}")
        except Exception as e:
            print(f"Error: {e}")

    # ════════════════════════════════════════════════════════════════════════
    #  EXIT
    # ════════════════════════════════════════════════════════════════════════

    def do_exit(self, arg):
        '''exit  —  Disconnect from Chrome, kill browser, and exit CLI'''
        print("Disconnecting and closing Chrome...")
        try:
            self.browser.close()
            self.pw.stop()
        except Exception:
            pass
        if hasattr(self, 'chrome_process'):
            self.chrome_process.terminate()
        return True

    def emptyline(self) -> bool | None:
        pass

    def postcmd(self, stop, line):
        # Don't screenshot if the command was vision itself, or exit, or empty
        cmd_name = line.strip().split()[0] if line.strip() else ""
        if cmd_name not in ["vision", "exit", ""]:
            self.screenshot_counter += 1
            fname = f"screenshot_{self.screenshot_counter}.png"
            self.do_vision(fname)
        return stop


if __name__ == "__main__":
    cli = BrowserCLI()
    try:
        cli.cmdloop()
    except KeyboardInterrupt:
        print("\nExiting...")
        cli.do_exit("")
        sys.exit(0)
