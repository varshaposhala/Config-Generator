import streamlit as st
import pandas as pd
import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, WebDriverException

# ============================================================
# SMART POLLING UTILITIES  (replaces fixed time.sleep)
# ============================================================

def _poll(condition_fn, timeout=3.0, interval=0.05, default=False):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = condition_fn()
            if result:
                return result
        except:
            pass
        time.sleep(interval)
    return default


def poll_url_changed(driver, old_url, timeout=5.0):
    return _poll(lambda: driver.current_url != old_url, timeout=timeout)


def poll_element_gone(driver, xpath, timeout=3.0):
    def check():
        els = driver.find_elements(By.XPATH, xpath)
        return not any(e.is_displayed() for e in els)
    return _poll(check, timeout=timeout)


def poll_element_visible(driver, xpath, timeout=5.0):
    def check():
        els = driver.find_elements(By.XPATH, xpath)
        return any(e.is_displayed() for e in els)
    return _poll(check, timeout=timeout)


def poll_button_visible(driver, text_fragment, timeout=5.0):
    def check():
        btns = driver.execute_script("""
            var frag = arguments[0].toLowerCase();
            var btns = document.querySelectorAll('button');
            for (var i=0; i<btns.length; i++){
                if((btns[i].innerText||'').toLowerCase().indexOf(frag)!==-1
                    && btns[i].offsetParent) return btns[i];
            }
            return null;
        """, text_fragment)
        return btns
    return _poll(check, timeout=timeout)


def poll_input_visible(driver, placeholder_or_type, timeout=3.0):
    def check():
        el = driver.execute_script("""
            var val = arguments[0];
            var inputs = document.querySelectorAll('input');
            for(var i=0;i<inputs.length;i++){
                var p = (inputs[i].placeholder||'').toLowerCase();
                var t = (inputs[i].type||'').toLowerCase();
                if((p.indexOf(val.toLowerCase())!==-1 || t===val.toLowerCase())
                    && inputs[i].offsetParent) return inputs[i];
            }
            return null;
        """, placeholder_or_type)
        return el
    return _poll(check, timeout=timeout)


def poll_popup_closed(driver, timeout=3.0):
    return poll_element_gone(
        driver,
        "//*[normalize-space(text())='Question Library']",
        timeout=timeout)


def poll_popup_open(driver, timeout=5.0):
    def check():
        try:
            els = driver.find_elements(By.XPATH,
                "//h2[normalize-space(text())='Add Questions'] | "
                "//*[@role='dialog']//*[normalize-space(text())='Add Questions'] | "
                "//*[normalize-space(text())='Question Library']")
            return any(e.is_displayed() for e in els)
        except:
            return False
    return _poll(check, timeout=timeout)


def poll_section_type_modal_gone(driver, timeout=3.0):
    return poll_element_gone(
        driver,
        "//*[contains(normalize-space(.),'Select Section Type')]",
        timeout=timeout)


def poll_options_visible(driver, timeout=3.0):
    def check():
        return driver.execute_script("""
            var sels=['[class*="Select__option"]','[class*="__option"]',
                      '[class*="-option"]','[role="option"]','div[id*="option"]'];
            for(var s=0;s<sels.length;s++){
                var opts=document.querySelectorAll(sels[s]);
                for(var i=0;i<opts.length;i++) if(opts[i].offsetParent) return true;
            }
            return false;
        """)
    return _poll(check, timeout=timeout, interval=0.03)

# ============================================================
# PAGE CONFIG
# ============================================================
st.set_page_config(page_title="Assessment Architect", page_icon="⚡", layout="wide")
st.markdown(
    "<h1 style='text-align: center; color: #FF4B4B;'>ASSESSMENT ARCHITECT ⚡ FAST</h1>",
    unsafe_allow_html=True
)

# ============================================================
# UI LAYOUT
# ============================================================
col1, col2 = st.columns([1, 1])
with col1:
    st.subheader("📁 1. Load Data")
    uploaded_file = st.file_uploader(
        "Upload Assessment CSV (Single or Multi-Section)", type=["csv"], key="assessment_csv"
    )
with col2:
    st.subheader("🔐 2. Portal Access")
    mob     = st.text_input("Mobile Number", placeholder="9876543210")
    otp_val = st.text_input("One Time Passcode", placeholder="6-digit OTP", max_chars=6)

st.subheader("⚙️ 3. Settings")
wait_time = st.slider("Element Wait Time (seconds)", 5, 30, 10)

# ============================================================
# CSV PARSER
# ============================================================

def parse_sections_from_csv(raw_df):
    """
    Parses a multi-section CSV into a list of section dicts.

    Handles the real CSV layout where Coding Question Restriction and
    Default Coding Language rows appear AFTER a blank separator row that
    follows the question data rows — i.e. they land in a separate block
    with no Section Type and would normally be discarded.  We detect those
    orphan metadata rows and merge them back into the most recently parsed
    section.
    """
    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _is_blank_row(row):
        return all(str(v).strip().lower() in ["", "nan"] for v in row)

    CODING_META_LABELS = {
        "coding question restriction", "coding restriction",
        "select coding language", "coding languages",
        "default coding language", "default language", "default coding lang",
    }

    def _is_coding_meta_row(row):
        lbl = str(row[0]).lower().strip()
        return any(kw in lbl for kw in CODING_META_LABELS) or lbl in CODING_META_LABELS

    SECTION_META_LABELS = {
        "section type", "name of section", "name of the section",
        "time limit (in mins)", "time limit",
    }

    def _is_section_meta_row(row):
        lbl = str(row[0]).lower().strip()
        return (lbl in SECTION_META_LABELS or
                "section type" in lbl or
                ("name" in lbl and "section" in lbl and "question" not in lbl) or
                ("time" in lbl and "limit" in lbl))

    def _parse_coding_meta(row, sections):
        """Apply a coding-meta row to the last parsed section."""
        if not sections:
            return
        lbl  = str(row[0]).lower().strip()
        val  = (str(row[1]).strip()
                if len(row) > 1 and str(row[1]).strip().lower() != "nan"
                else "")
        sec  = sections[-1]

        if ("coding question restriction" in lbl or
                "coding restriction" in lbl or
                "select coding language" in lbl or
                lbl == "coding languages"):
            raw_vals = []
            for c in range(1, len(row)):
                cell = str(row[c]).strip()
                if cell.lower() not in ["", "nan"]:
                    for part in cell.split(","):
                        part = part.strip()
                        if part and part.lower() not in ["", "nan"]:
                            raw_vals.append(part)
            sec["coding_restriction"] = ", ".join(raw_vals)

        elif ("default coding language" in lbl or
              "default language" in lbl or
              lbl == "default coding lang"):
            if val and "," in val:
                sec["default_coding_lang"] = val.split(",")[0].strip()
            else:
                sec["default_coding_lang"] = val

    # ------------------------------------------------------------------ #
    # parse_block — reads one section block (metadata + question rows)
    # ------------------------------------------------------------------ #
    def parse_block(block_rows):
        section_type        = ""
        section_name        = ""
        time_limit          = ""
        coding_restriction  = ""
        default_coding_lang = ""
        header_idx          = None

        def _is_metadata_row(row):
            lbl = str(row[0]).lower().strip()
            return (_is_section_meta_row(row) or _is_coding_meta_row(row))

        # Scan ALL rows — never break early
        for i, row in enumerate(block_rows):
            label = str(row[0]).lower().strip()
            val   = (str(row[1]).strip()
                     if len(row) > 1 and str(row[1]).strip().lower() != "nan"
                     else "")

            if "section type" in label:
                section_type = val

            elif ("name" in label and "section" in label
                  and "question" not in label):
                section_name = val

            elif "time" in label and "limit" in label:
                time_limit = val

            elif ("coding question restriction" in label or
                  "coding restriction" in label or
                  "select coding language" in label or
                  label == "coding languages"):
                raw_vals = []
                for c in range(1, len(row)):
                    cell = str(row[c]).strip()
                    if cell.lower() not in ["", "nan"]:
                        for part in cell.split(","):
                            part = part.strip()
                            if part and part.lower() not in ["", "nan"]:
                                raw_vals.append(part)
                coding_restriction = ", ".join(raw_vals)

            elif ("default coding language" in label or
                  "default language" in label or
                  label == "default coding lang"):
                if val and "," in val:
                    default_coding_lang = val.split(",")[0].strip()
                else:
                    default_coding_lang = val

            elif "question library" in label and header_idx is None:
                header_idx = i  # record but DO NOT break

        if not section_type:
            return None

        questions_df = pd.DataFrame()
        if header_idx is not None:
            raw_header = [
                str(block_rows[header_idx][c]).strip()
                if str(block_rows[header_idx][c]).strip().lower() not in ["", "nan"]
                else f"Unnamed_{c}"
                for c in range(len(block_rows[header_idx]))
            ]

            seen_num_q = False
            exclusive_tag_count = 0
            header = []
            for col in raw_header:
                col_lower = col.lower().strip()
                if col_lower in ("exclusive tags", "exclusive tags (optional)",
                                 "exclusive tag", "exclusive tag (optional)") or \
                   col_lower.startswith("exclusive tags-") or \
                   col_lower.startswith("exclusive tag-"):
                    exclusive_tag_count += 1
                    if exclusive_tag_count == 1:
                        header.append("Exclusive Tags (Optional)")
                    else:
                        header.append(f"Exclusive Tags-{exclusive_tag_count}")
                elif col_lower == "number of questions" and not seen_num_q:
                    seen_num_q = True
                    header.append("Number of Questions")
                elif col_lower == "number of questions" and seen_num_q:
                    header.append("Marks for Each Question")
                else:
                    header.append(col)

            # Exclude metadata rows that may appear after the question rows
            data_rows = [
                row for row in block_rows[header_idx + 1:]
                if not _is_metadata_row(row)
            ]
            if data_rows:
                questions_df = pd.DataFrame(data_rows, columns=header)
                main_cols = [
                    "Question Library", "Topic", "Difficulty Level",
                    "Sub Topic", "Number of Questions", "Marks for Each Question"
                ]
                for col in questions_df.columns:
                    if col.startswith("Exclusive Tags"):
                        main_cols.append(col)
                existing = [c for c in main_cols if c in questions_df.columns]
                if existing:
                    questions_df = questions_df.dropna(how="all", subset=existing)
                questions_df = questions_df.reset_index(drop=True)

        return {
            "section_type":        section_type,
            "section_name":        section_name,
            "time_limit":          time_limit,
            "coding_restriction":  coding_restriction,
            "default_coding_lang": default_coding_lang,
            "questions_df":        questions_df,
        }

    # ------------------------------------------------------------------ #
    # Main loop — split raw_df into blocks and parse each one.
    # After a blank row, peek at the next rows:
    #   • If they are coding-meta rows (no Section Type), merge them into
    #     the last parsed section rather than creating a new orphan block.
    #   • Otherwise treat the blank as a normal section separator.
    # ------------------------------------------------------------------ #
    sections      = []
    current_block = []
    all_rows      = [list(r) for _, r in raw_df.iterrows()]
    n             = len(all_rows)
    i             = 0

    while i < n:
        row = all_rows[i]

        if _is_blank_row(row):
            # Flush current block if it has content
            if current_block:
                parsed = parse_block(current_block)
                if parsed:
                    sections.append(parsed)
                current_block = []

            # Peek ahead: collect consecutive non-blank rows that follow
            j = i + 1
            peek_rows = []
            while j < n and not _is_blank_row(all_rows[j]):
                peek_rows.append(all_rows[j])
                j += 1

            # If ALL peeked rows are coding-meta (no new Section Type found),
            # merge them directly into the last section instead of a new block
            has_section_type = any(
                "section type" in str(r[0]).lower().strip()
                for r in peek_rows
            )
            if peek_rows and not has_section_type:
                # All these rows are orphan metadata — apply to last section
                for meta_row in peek_rows:
                    if _is_coding_meta_row(meta_row):
                        _parse_coding_meta(meta_row, sections)
                i = j  # skip past these rows
                continue

            # Normal blank separator — just advance past the blank
            i += 1

        else:
            label = str(row[0]).lower().strip()
            # If we see a new Section Type mid-block, flush first
            if "section type" in label and current_block:
                parsed = parse_block(current_block)
                if parsed:
                    sections.append(parsed)
                current_block = []
            current_block.append(row)
            i += 1

    # Flush any remaining block
    if current_block:
        parsed = parse_block(current_block)
        if parsed:
            sections.append(parsed)

    return sections


# ============================================================
# SAFE INPUT HELPERS
# ============================================================

def _safe_send(driver, el, text):
    try:
        driver.execute_script(
            "arguments[0].removeAttribute('readonly');"
            "arguments[0].removeAttribute('disabled');"
            "arguments[0].scrollIntoView({block:'center'});"
            "arguments[0].focus();", el)
    except:
        pass
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(driver).move_to_element(el).click().perform()
    except:
        pass
    try:
        el.send_keys(text)
        return
    except:
        pass
    try:
        driver.execute_script("""
            var el  = arguments[0];
            var val = arguments[1];
            var s   = Object.getOwnPropertyDescriptor(
                          window.HTMLInputElement.prototype, 'value').set;
            s.call(el, val);
            ['input', 'change'].forEach(function(e) {
                el.dispatchEvent(new Event(e, {bubbles: true}));
            });
            el.dispatchEvent(new KeyboardEvent('keydown', {key: val, bubbles: true}));
            el.dispatchEvent(new KeyboardEvent('keyup',   {key: val, bubbles: true}));
        """, el, str(text))
    except:
        pass


def _safe_key(driver, el, key_name):
    key = getattr(Keys, key_name, key_name)
    try:
        el.send_keys(key)
        return
    except:
        pass
    code_map = {
        'RETURN': 13, 'ESCAPE': 27, 'TAB': 9,
        'DELETE': 46, 'BACK_SPACE': 8
    }
    kc = code_map.get(key_name, 0)
    try:
        driver.execute_script("""
            var el = arguments[0], kc = arguments[1];
            ['keydown', 'keypress', 'keyup'].forEach(function(t) {
                el.dispatchEvent(new KeyboardEvent(t, {
                    bubbles: true, cancelable: true, keyCode: kc, which: kc
                }));
            });
        """, el, kc)
    except:
        pass


# ============================================================
# CORE UTILITY FUNCTIONS
# ============================================================

def find_and_click(driver, xpath, timeout=8):
    try:
        el = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, xpath)))
        driver.execute_script("arguments[0].click();", el)
        return True
    except:
        return False


# ============================================================
# POPUP STATE HELPERS
# ============================================================

def popup_is_open(driver):
    try:
        els = driver.find_elements(
            By.XPATH, "//*[normalize-space(text())='Question Library']")
        return any(e.is_displayed() for e in els)
    except:
        return False


def wait_for_popup_open(driver, timeout=10):
    for _ in range(timeout * 5):
        if popup_is_open(driver):
            return True
        time.sleep(0.2)
    return False


def wait_for_popup_closed(driver, timeout=10):
    for _ in range(timeout * 5):
        if not popup_is_open(driver):
            return True
        time.sleep(0.2)
    return False


# ============================================================
# REACT-SELECT HELPERS
# ============================================================

def _close_open_menus(driver):
    try:
        driver.execute_script("""
            document.querySelectorAll('[class*="__menu"],[class*="-menu"]').forEach(function(m){
                if (m.offsetParent) m.style.display = 'none';
            });
        """)
    except:
        pass
    try:
        driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
    except:
        pass


def _wait_for_options(driver, timeout=4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = driver.execute_script("""
            var selectors = [
                '[class*="Select__option"]',
                '[class*="__option"]',
                '[class*="-option"]',
                '[role="option"]',
                'div[id*="option"]'
            ];
            for (var s = 0; s < selectors.length; s++) {
                var opts = document.querySelectorAll(selectors[s]);
                for (var i = 0; i < opts.length; i++) {
                    if (opts[i].offsetParent) return true;
                }
            }
            return false;
        """)
        if found:
            return True
        time.sleep(0.1)
    return False


def _click_option_in_menu(driver, target_text):
    return driver.execute_script("""
        var target = arguments[0].toLowerCase().trim();
        var selectors = [
            '[class*="Select__option"]',
            '[class*="__option"]',
            '[class*="-option"]',
            '[role="option"]',
            'div[id*="option"]'
        ];
        for (var s = 0; s < selectors.length; s++) {
            var opts = document.querySelectorAll(selectors[s]);
            for (var i = 0; i < opts.length; i++) {
                var opt = opts[i];
                if (!opt.offsetParent) continue;
                var txt = (opt.innerText || opt.textContent || '').trim().toLowerCase();
                if (txt === target) {
                    opt.scrollIntoView({block:'nearest'});
                    opt.click();
                    return true;
                }
            }
        }
        for (var s2 = 0; s2 < selectors.length; s2++) {
            var opts2 = document.querySelectorAll(selectors[s2]);
            for (var j = 0; j < opts2.length; j++) {
                var opt2 = opts2[j];
                if (!opt2.offsetParent) continue;
                var t2 = (opt2.innerText || opt2.textContent || '').trim().toLowerCase();
                if (t2.indexOf(target) !== -1) {
                    opt2.scrollIntoView({block:'nearest'});
                    opt2.click();
                    return true;
                }
            }
        }
        return false;
    """, target_text)


# ============================================================
# FIX 1 OF 3: ensure_tab_focus
# ============================================================

def ensure_tab_focus(driver):
    try:
        driver.execute_script("""
            document.__proto__.hasFocus = function(){ return true; };
            document.hasFocus           = function(){ return true; };
            try {
                Object.defineProperty(document, 'visibilityState', {
                    get: function(){ return 'visible'; }, configurable: true });
                Object.defineProperty(document, 'hidden', {
                    get: function(){ return false; }, configurable: true });
            } catch(e) {}
            if (!window.__tabFixApplied) {
                window.__tabFixApplied = true;
                window.addEventListener('visibilitychange', function(e){
                    e.stopImmediatePropagation();
                }, true);
                window.addEventListener('blur', function(e){
                    e.stopImmediatePropagation();
                }, true);
            }
        """)
    except:
        pass
    return True


# ============================================================
# REACT-SELECT DROPDOWN HANDLER
# ============================================================

def click_react_select(driver, label_text, option_text, progress_placeholder):
    from selenium.webdriver.common.action_chains import ActionChains

    ensure_tab_focus(driver)
    progress_placeholder.info(f"  🎯 {label_text} → '{option_text}'")

    def close_open_menus():
        try:
            driver.execute_script("""
                document.querySelectorAll('[class*="menu"]').forEach(function(m){
                    if(m.offsetParent) m.style.display='none';
                });
            """)
        except:
            pass
        try:
            driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
        except:
            pass

    def find_control():
        ctrl = driver.execute_script("""
            var lbl = arguments[0];
            var all = document.querySelectorAll('label,p,span,div,h4,h5,h6,legend,li');
            for (var i = 0; i < all.length; i++) {
                var el = all[i];
                if (el.children.length > 0) continue;
                if (!el.offsetParent) continue;
                if ((el.innerText || el.textContent || '').trim() !== lbl) continue;
                var p = el.parentElement;
                for (var j = 0; j < 8; j++) {
                    if (!p) break;
                    var c = p.querySelector('[class*="-control"],[class*="__control"]');
                    if (c) return c;
                    p = p.parentElement;
                }
            }
            return null;
        """, label_text)
        if ctrl:
            return ctrl
        for xp in [
            f"//*[normalize-space(text())='{label_text}']/following::div[contains(@class,'control')][1]",
            f"//*[contains(text(),'{label_text}')]/following::div[contains(@class,'control')][1]",
        ]:
            try:
                el = driver.find_element(By.XPATH, xp)
                if el.is_displayed():
                    return el
            except:
                continue
        return None

    def open_and_type(control, text):
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", control)
        driver.execute_script("""
            var el = arguments[0];
            el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
            el.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true, cancelable:true, view:window}));
            el.dispatchEvent(new MouseEvent('click',     {bubbles:true, cancelable:true, view:window}));
        """, control)
        time.sleep(0.005)

        inp = driver.execute_script(
            "return arguments[0].querySelector('input');", control)
        if inp:
            try:
                driver.execute_script(
                    "arguments[0].removeAttribute('readonly');"
                    "arguments[0].removeAttribute('disabled');"
                    "arguments[0].scrollIntoView({block:'center'});"
                    "arguments[0].focus();", inp)
            except:
                pass
            try:
                driver.execute_script("""
                    var el = arguments[0];
                    var s = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    s.call(el, '');
                    el.dispatchEvent(new Event('input',  {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                """, inp)
            except:
                pass
            typed = False
            try:
                driver.execute_script("""
                    var el = arguments[0], val = arguments[1];
                    var s = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    s.call(el, val);
                    el.dispatchEvent(new Event('input',  {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    el.dispatchEvent(new KeyboardEvent('keydown', {key:val, bubbles:true}));
                    el.dispatchEvent(new KeyboardEvent('keyup',   {key:val, bubbles:true}));
                """, inp, text)
                typed = True
            except:
                pass
            if not typed:
                try:
                    driver.execute_script("""
                        var el = arguments[0], val = arguments[1];
                        el.focus();
                        for (var i = 0; i < val.length; i++) {
                            var ch = val[i];
                            el.dispatchEvent(new KeyboardEvent('keydown',
                                {key:ch, bubbles:true, cancelable:true}));
                            el.dispatchEvent(new KeyboardEvent('keypress',
                                {key:ch, bubbles:true, cancelable:true}));
                            el.dispatchEvent(new KeyboardEvent('keyup',
                                {key:ch, bubbles:true, cancelable:true}));
                        }
                    """, inp, text)
                    typed = True
                except:
                    pass
        else:
            try:
                driver.execute_script("""
                    document.activeElement.dispatchEvent(
                        new KeyboardEvent('keydown',
                            {key:'Enter',keyCode:13,bubbles:true,cancelable:true}));
                """)
            except:
                pass
        time.sleep(0.05)

    def pick_option(text):
        found = driver.execute_script("""
            var target = arguments[0].toLowerCase().trim();
            var selectors = [
                '[role="option"]',
                '[class*="Select__option"]',
                '[class*="__option"]',
                '[class*="-option"]',
                'div[id*="option"]'
            ];
            for (var s = 0; s < selectors.length; s++) {
                var opts = document.querySelectorAll(selectors[s]);
                for (var i = 0; i < opts.length; i++) {
                    var opt = opts[i];
                    if (!opt.offsetParent) continue;
                    var txt = (opt.innerText || opt.textContent || '').trim().toLowerCase();
                    if (txt === target || txt.indexOf(target) !== -1) {
                        opt.scrollIntoView({block:'nearest'});
                        opt.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));
                        opt.dispatchEvent(new MouseEvent('mouseup',  {bubbles:true,cancelable:true}));
                        opt.dispatchEvent(new MouseEvent('click',    {bubbles:true,cancelable:true}));
                        return true;
                    }
                }
            }
            return false;
        """, text)
        if found:
            return True
        try:
            driver.execute_script("""
                document.dispatchEvent(new KeyboardEvent('keydown',
                    {key:'Enter', keyCode:13, bubbles:true, cancelable:true}));
            """)
            return True
        except:
            pass
        return False

    def get_current_value(control):
        try:
            val = driver.execute_script("""
                var c = arguments[0];
                var sv = c.querySelector('[class*="single-value"]');
                if (sv) return (sv.innerText || sv.textContent || '').trim();
                return '';
            """, control)
            return (val or "").strip()
        except:
            return ""

    close_open_menus()
    control = find_control()
    if not control:
        progress_placeholder.warning(f"  ⚠️ Dropdown not found: '{label_text}'")
        return False

    for attempt in range(3):
        open_and_type(control, option_text)
        picked = pick_option(option_text)
        if not picked:
            progress_placeholder.warning(
                f"  ⚠️ Option '{option_text}' not found (attempt {attempt+1})")
            close_open_menus()
            time.sleep(0.02)
            continue
        current = get_current_value(control)
        if option_text.lower() in current.lower():
            progress_placeholder.info(f"  ✅ {label_text} = '{current}'")
            return True
        else:
            progress_placeholder.warning(
                f"  ⚠️ Mismatch: expected '{option_text}', got '{current}' — retrying")
            close_open_menus()
            time.sleep(0.01)

    progress_placeholder.warning(
        f"  ❌ Could not set '{label_text}' to '{option_text}' after 3 attempts")
    return False


# ============================================================
# QUESTION LIBRARY HANDLER
# ============================================================

def handle_question_library(driver, qlib, progress_placeholder):
    csv_val = qlib.strip()
    if csv_val.lower() in ["topin questions", "topin", ""]:
        progress_placeholder.info("  ✅ Question Library = Topin Questions (default, skipping)")
        return True

    current = driver.execute_script("""
        var all = document.querySelectorAll('*');
        for (var i = 0; i < all.length; i++) {
            var el = all[i];
            if (el.children.length > 0) continue;
            if ((el.innerText || el.textContent || '').trim() !== 'Question Library') continue;
            var p = el.parentElement;
            for (var j = 0; j < 8; j++) {
                if (!p) break;
                var sv = p.querySelector('[class*="single-value"]');
                if (sv) return (sv.innerText || sv.textContent || '').trim();
                p = p.parentElement;
            }
        }
        return '';
    """) or ""

    progress_placeholder.info(f"  📚 Question Library: current='{current}' → target='{csv_val}'")
    if csv_val.lower() in current.lower():
        progress_placeholder.info(f"  ✅ Already = '{current}' — skipping")
        return True
    return click_react_select(driver, "Question Library", csv_val, progress_placeholder)


# ============================================================
# EXCLUSIVE TAGS
# ============================================================

def set_exclusive_tags(driver, tags_str, progress_placeholder):
    from selenium.webdriver.common.action_chains import ActionChains
    try:
        tags = []
        for t in str(tags_str).split(','):
            cleaned = t.strip()
            if cleaned and cleaned.lower() not in ['nan','none','null','']:
                tags.append(cleaned)

        if not tags:
            progress_placeholder.info("  🏷️ No exclusive tags to add")
            return True

        progress_placeholder.info(f"  🏷️ Adding {len(tags)} exclusive tags: {tags}")

        tag_input = None
        for xp in [
            "//input[@placeholder='Add Tags']",
            "//input[contains(@placeholder,'Add Tags')]",
            "//input[contains(@placeholder,'Tag')]",
            "//*[normalize-space(text())='Exclusive Tags (Optional)']/following::input[1]",
            "//*[contains(text(),'Exclusive Tags')]/following::input[1]",
        ]:
            try:
                el = driver.find_element(By.XPATH, xp)
                if el.is_displayed():
                    tag_input = el
                    break
            except:
                continue

        if not tag_input:
            progress_placeholder.warning("  ⚠️ Tag input field not found")
            return False

        for i, tag in enumerate(tags, 1):
            progress_placeholder.info(f"    🏷️ Adding tag {i}/{len(tags)}: '{tag}'")
            ensure_tab_focus(driver)
            try:
                driver.execute_script(
                    "arguments[0].removeAttribute('readonly');"
                    "arguments[0].removeAttribute('disabled');"
                    "arguments[0].scrollIntoView({block:'center'});"
                    "arguments[0].focus();", tag_input)
                time.sleep(0.03)
                ActionChains(driver).move_to_element(tag_input).click().perform()
                time.sleep(0.03)
            except:
                pass

            _safe_key(driver, tag_input, 'BACK_SPACE')
            _safe_send(driver, tag_input, tag)
            time.sleep(0.08)
            _safe_key(driver, tag_input, 'RETURN')
            time.sleep(0.08)
            progress_placeholder.info(f"    ✅ Tag added: '{tag}'")

        try:
            driver.execute_script("arguments[0].blur();", tag_input)
        except:
            pass
        progress_placeholder.success(f"  ✅ All {len(tags)} exclusive tags added successfully")
        return True

    except Exception as e:
        progress_placeholder.warning(f"  ⚠️ Exclusive tags error: {str(e)[:100]}")
        return False


# ============================================================
# NEW HELPER: dismiss_tag_overlays
# Aggressively removes focus and any open tag suggestion dropdowns
# that can sit on top of the "Add Questions" button after tagging.
# Called immediately after set_exclusive_tags() completes.
# ============================================================

def dismiss_tag_overlays(driver, progress_placeholder):
    """
    After exclusive tags are entered, the tag input may still hold focus
    and a suggestion/autocomplete dropdown may be open. This function:
      1. Blurs every tag-related input
      2. Hides any visible suggestion/autocomplete/dropdown overlays
      3. Fires Escape at body level to close any remaining overlay
      4. Clicks the body to reset focus to a neutral element
    This ensures nothing intercepts the 'Add Questions' button click.
    """
    try:
        driver.execute_script("""
            // 1. Blur the active element (likely the tag input)
            if (document.activeElement) {
                document.activeElement.blur();
                document.activeElement.dispatchEvent(new Event('blur', {bubbles: true}));
            }

            // 2. Blur every tag-related input explicitly
            var tagSelectors = [
                'input[placeholder*="Tag"]',
                'input[placeholder*="tag"]',
                'input[placeholder*="Add Tag"]',
                'input[placeholder*="add tag"]'
            ];
            for (var s = 0; s < tagSelectors.length; s++) {
                var inputs = document.querySelectorAll(tagSelectors[s]);
                for (var i = 0; i < inputs.length; i++) {
                    inputs[i].blur();
                    inputs[i].dispatchEvent(new Event('blur', {bubbles: true}));
                }
            }

            // 3. Hide any visible overlay that could sit on top of the button
            var overlaySelectors = [
                '[class*="suggestion"]',
                '[class*="autocomplete"]',
                '[class*="tag-suggest"]',
                '[class*="Tag__menu"]',
                '[class*="tags__menu"]',
                '[class*="TagInput__dropdown"]',
                '[class*="tag-dropdown"]',
                '[class*="__dropdown"]',
                '[class*="-dropdown"]',
                '[class*="__menu"]',
                '[class*="-menu"]'
            ];
            for (var o = 0; o < overlaySelectors.length; o++) {
                var overlays = document.querySelectorAll(overlaySelectors[o]);
                for (var j = 0; j < overlays.length; j++) {
                    if (overlays[j].offsetParent) {
                        overlays[j].style.display = 'none';
                        overlays[j].style.visibility = 'hidden';
                        overlays[j].style.pointerEvents = 'none';
                    }
                }
            }

            // 4. Click body at a neutral spot to reset focus
            document.body.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
            document.body.dispatchEvent(new MouseEvent('mouseup',   {bubbles: true, cancelable: true}));
            document.body.dispatchEvent(new MouseEvent('click',     {bubbles: true, cancelable: true}));
        """)
    except:
        pass

    # 5. Fire Escape via Selenium as an additional overlay dismissal
    try:
        driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
    except:
        pass

    # 6. One more activeElement blur after Escape
    try:
        driver.execute_script(
            "if (document.activeElement) document.activeElement.blur();")
    except:
        pass

    progress_placeholder.info("  🧹 Tag overlays dismissed — ready for button click")


# ============================================================
# NUMBER OF QUESTIONS
# ============================================================

def set_number_of_questions(driver, value, progress_placeholder):
    from selenium.webdriver.common.action_chains import ActionChains
    try:
        progress_placeholder.info(f"  🔢 Number of Questions → {value}")
        inp = None

        inp = driver.execute_script("""
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {
                var el = all[i];
                if (el.children.length > 0 || !el.offsetParent) continue;
                if ((el.innerText || el.textContent || '').trim() !== 'Number of Questions') continue;
                var p = el.parentElement;
                for (var j = 0; j < 8; j++) {
                    if (!p) break;
                    var sib = p.nextElementSibling;
                    while (sib) {
                        var f = sib.tagName === 'INPUT' ? sib : sib.querySelector('input');
                        if (f && f.offsetParent) return f;
                        sib = sib.nextElementSibling;
                    }
                    var c = p.querySelector('input');
                    if (c && c.offsetParent && c !== el) return c;
                    p = p.parentElement;
                }
            }
            return null;
        """)

        if not inp:
            inp = driver.execute_script("""
                var a = document.querySelectorAll('input[placeholder="0"]');
                for (var i = 0; i < a.length; i++) if (a[i].offsetParent) return a[i];
                return null;
            """)
        if not inp:
            inp = driver.execute_script("""
                var a = document.querySelectorAll('input[type="number"]');
                for (var i = 0; i < a.length; i++) if (a[i].offsetParent) return a[i];
                return null;
            """)
        if not inp:
            visible = [x for x in driver.find_elements(By.TAG_NAME, "input") if x.is_displayed()]
            if visible:
                inp = visible[-1]

        if not inp:
            progress_placeholder.warning("  ⚠️ Number of Questions input not found")
            return False

        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", inp)
        time.sleep(0.05)
        try:
            ActionChains(driver).move_to_element(inp).click().perform()
        except:
            driver.execute_script("arguments[0].click();", inp)
        time.sleep(0.08)

        try:
            inp.send_keys(Keys.CONTROL + "a")
            time.sleep(0.05)
            inp.send_keys(Keys.DELETE)
            time.sleep(0.05)
        except:
            pass

        try:
            inp.send_keys(str(value))
            time.sleep(0.08)
            progress_placeholder.info(f"    ✓ Typed via send_keys: '{value}'")
        except:
            driver.execute_script("""
                var el = arguments[0], val = arguments[1];
                var nativeSet = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                el.focus();
                nativeSet.call(el, '');
                el.dispatchEvent(new Event('input',  {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                nativeSet.call(el, val);
                el.dispatchEvent(new Event('input',  {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            """, inp, str(value))
            time.sleep(0.08)
            progress_placeholder.info(f"    ✓ Set via JS: '{value}'")

        actual = inp.get_attribute("value") or ""
        progress_placeholder.info(f"  ✅ Number of Questions = '{actual}'")
        try:
            driver.execute_script("arguments[0].blur();", inp)
        except:
            pass
        time.sleep(0.08)
        return True

    except Exception as e:
        progress_placeholder.warning(f"  ⚠️ Num Q error: {str(e)[:120]}")
        return False


# ============================================================
# MARKS FOR EACH QUESTION
# ============================================================

MARKS_SECTION_TYPES = {"coding", "ide based coding", "sql", "sql coding", "web coding", "textual"}


def set_marks_per_question(driver, value, progress_placeholder):
    from selenium.webdriver.common.action_chains import ActionChains
    try:
        progress_placeholder.info(f"  🏅 Marks for Each Question → {value}")
        inp = None

        inp = driver.execute_script("""
            var labelTexts = [
                'Marks for Each Question',
                'Marks Per Question',
                'Marks/Question',
                'Mark for Each Question',
                'Marks'
            ];
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {
                var el = all[i];
                if (el.children.length > 0 || !el.offsetParent) continue;
                var txt = (el.innerText || el.textContent || '').trim();
                var matched = false;
                for (var k = 0; k < labelTexts.length; k++) {
                    if (txt === labelTexts[k]) { matched = true; break; }
                }
                if (!matched) continue;
                var p = el.parentElement;
                for (var j = 0; j < 8; j++) {
                    if (!p) break;
                    var sib = p.nextElementSibling;
                    while (sib) {
                        var f = sib.tagName === 'INPUT' ? sib : sib.querySelector('input');
                        if (f && f.offsetParent) return f;
                        sib = sib.nextElementSibling;
                    }
                    var c = p.querySelector('input');
                    if (c && c.offsetParent && c !== el) return c;
                    p = p.parentElement;
                }
            }
            return null;
        """)

        if not inp:
            inp = driver.execute_script("""
                var inputs = document.querySelectorAll('input[type="number"]');
                var visible = [];
                for (var i = 0; i < inputs.length; i++) {
                    if (inputs[i].offsetParent) visible.push(inputs[i]);
                }
                return visible.length >= 2 ? visible[1] : null;
            """)

        if not inp:
            for xp in [
                "//*[normalize-space(text())='Marks for Each Question']/following::input[1]",
                "//*[contains(text(),'Marks for Each Question')]/following::input[1]",
                "//*[contains(text(),'Marks Per Question')]/following::input[1]",
            ]:
                try:
                    el = driver.find_element(By.XPATH, xp)
                    if el.is_displayed():
                        inp = el
                        break
                except:
                    continue

        if not inp:
            progress_placeholder.warning(
                "  ⚠️ Marks for Each Question input not found — skipping")
            return False

        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", inp)
        time.sleep(0.05)
        try:
            ActionChains(driver).move_to_element(inp).click().perform()
        except:
            driver.execute_script("arguments[0].click();", inp)
        time.sleep(0.08)

        try:
            inp.send_keys(Keys.CONTROL + "a")
            time.sleep(0.05)
            inp.send_keys(Keys.DELETE)
            time.sleep(0.05)
        except:
            pass
        try:
            driver.execute_script("""
                var el = arguments[0];
                var nativeSet = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                nativeSet.call(el, '');
                el.dispatchEvent(new Event('input',  {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            """, inp)
        except:
            pass

        typed = False
        try:
            inp.send_keys(str(value))
            time.sleep(0.08)
            progress_placeholder.info(f"    ✓ Typed via send_keys: '{value}'")
            typed = True
        except:
            pass

        if not typed:
            try:
                driver.execute_script("""
                    var el = arguments[0], val = arguments[1];
                    var nativeSet = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    el.focus();
                    nativeSet.call(el, '');
                    el.dispatchEvent(new Event('input',  {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    nativeSet.call(el, val);
                    el.dispatchEvent(new Event('input',  {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                """, inp, str(value))
                time.sleep(0.08)
                progress_placeholder.info(f"    ✓ Set via JS native setter: '{value}'")
                typed = True
            except:
                pass

        if not typed:
            try:
                ActionChains(driver).move_to_element(inp).click().send_keys(str(value)).perform()
                time.sleep(0.08)
                progress_placeholder.info(f"    ✓ Set via ActionChains: '{value}'")
            except:
                pass

        actual = inp.get_attribute("value") or ""
        progress_placeholder.info(f"  ✅ Marks for Each Question = '{actual}'")
        try:
            driver.execute_script("arguments[0].blur();", inp)
        except:
            pass
        time.sleep(0.08)
        return True

    except Exception as e:
        progress_placeholder.warning(f"  ⚠️ Marks error: {str(e)[:120]}")
        return False


# ============================================================
# HELPER
# ============================================================

def _get_all_visible_controls_sorted(driver):
    return driver.execute_script("""
        var controls = document.querySelectorAll(
            '[class*="__control"],[class*="-control"]');
        var visible = [];
        for (var i = 0; i < controls.length; i++) {
            var c = controls[i];
            if (!c.offsetParent) continue;
            var r = c.getBoundingClientRect();
            if (r.width > 0 && r.height > 0)
                visible.push(c);
        }
        visible.sort(function(a, b) {
            return a.getBoundingClientRect().left - b.getBoundingClientRect().left;
        });
        return visible;
    """)


# ============================================================
# CODING QUESTION RESTRICTION
# Fixed: replaced fire-and-forget setTimeout with a synchronous
# Python polling loop so every language is confirmed selected
# before moving on. Same overlay-dismissal pattern as tags fix.
# ============================================================

# ============================================================
# CODING DROPDOWN HELPERS
# ============================================================

LANG_MAP = {
    'python': 'Python', 'python3': 'Python',
    'java': 'Java', 'c': 'C',
    'c++': 'C++', 'cpp': 'C++', 'cplusplus': 'C++',
    'c#': 'C#', 'csharp': 'C#',
    'javascript': 'JavaScript', 'js': 'JavaScript',
    'ruby': 'Ruby', 'go': 'Go', 'swift': 'Swift',
    'kotlin': 'Kotlin', 'scala': 'Scala', 'r': 'R',
}


def _dismiss_coding_overlays(driver):
    """Closes any open dropdown menus and resets focus."""
    try:
        driver.execute_script("""
            if (document.activeElement) document.activeElement.blur();
            var sels = [
                '[class*="__menu"]', '[class*="-menu"]',
                '[class*="__dropdown"]', '[class*="-dropdown"]',
                '[class*="suggestion"]', '[class*="autocomplete"]'
            ];
            for (var o = 0; o < sels.length; o++) {
                var els = document.querySelectorAll(sels[o]);
                for (var j = 0; j < els.length; j++) {
                    if (els[j].offsetParent) {
                        els[j].style.display = 'none';
                        els[j].style.visibility = 'hidden';
                        els[j].style.pointerEvents = 'none';
                    }
                }
            }
            document.body.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
            document.body.dispatchEvent(new MouseEvent('click',     {bubbles:true}));
        """)
    except:
        pass
    try:
        driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
    except:
        pass


def _open_dropdown_by_element(driver, el):
    """Fires mouse events on el to open its dropdown, then polls until options appear."""
    driver.execute_script("""
        var el = arguments[0];
        el.scrollIntoView({block: 'center'});
        el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
        el.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true, cancelable:true, view:window}));
        el.dispatchEvent(new MouseEvent('click',     {bubbles:true, cancelable:true, view:window}));
        var inp = el.querySelector ? el.querySelector('input') : null;
        if (!inp && el.tagName === 'INPUT') inp = el;
        if (inp) inp.focus();
    """, el)
    # Poll for options up to 2 seconds
    deadline = time.time() + 2.0
    while time.time() < deadline:
        visible = driver.execute_script("""
            var sels = ['[role="option"]','[class*="Select__option"]',
                        '[class*="__option"]','[class*="-option"]'];
            for (var s = 0; s < sels.length; s++) {
                var opts = document.querySelectorAll(sels[s]);
                for (var i = 0; i < opts.length; i++)
                    if (opts[i].offsetParent) return true;
            }
            return false;
        """)
        if visible:
            return True
        time.sleep(0.05)
    return False


def _click_visible_option(driver, target_text):
    """
    Finds a visible dropdown option matching target_text (exact then partial)
    and clicks it via JS mouse events. Returns True if clicked.
    """
    return driver.execute_script("""
        var target = arguments[0].toLowerCase().trim();
        var sels = ['[role="option"]','[class*="Select__option"]',
                    '[class*="__option"]','[class*="-option"]','li'];
        // Exact match first
        for (var s = 0; s < sels.length; s++) {
            var opts = document.querySelectorAll(sels[s]);
            for (var i = 0; i < opts.length; i++) {
                var opt = opts[i];
                if (!opt.offsetParent) continue;
                var txt = (opt.innerText || opt.textContent || '').trim().toLowerCase();
                if (txt === target) {
                    opt.scrollIntoView({block:'nearest'});
                    opt.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));
                    opt.dispatchEvent(new MouseEvent('mouseup',  {bubbles:true,cancelable:true}));
                    opt.dispatchEvent(new MouseEvent('click',    {bubbles:true,cancelable:true}));
                    return true;
                }
            }
        }
        // Partial match fallback
        for (var s = 0; s < sels.length; s++) {
            var opts = document.querySelectorAll(sels[s]);
            for (var i = 0; i < opts.length; i++) {
                var opt = opts[i];
                if (!opt.offsetParent) continue;
                var txt = (opt.innerText || opt.textContent || '').trim().toLowerCase();
                if (txt.indexOf(target) !== -1) {
                    opt.scrollIntoView({block:'nearest'});
                    opt.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));
                    opt.dispatchEvent(new MouseEvent('mouseup',  {bubbles:true,cancelable:true}));
                    opt.dispatchEvent(new MouseEvent('click',    {bubbles:true,cancelable:true}));
                    return true;
                }
            }
        }
        return false;
    """, target_text)


def _find_element_by_label(driver, label_text, max_depth=12):
    """
    Walks up from any visible leaf element whose text equals label_text,
    then searches siblings/descendants for the nearest React-Select control
    or plain <select>. Returns the clickable control element or None.
    Works on both multi-select and single-select dropdowns.
    """
    return driver.execute_script("""
        var label   = arguments[0];
        var maxDep  = arguments[1];

        // Collect every visible leaf whose text matches
        var all = document.querySelectorAll('label,p,span,div,h4,h5,h6,li,legend,td,th');
        for (var i = 0; i < all.length; i++) {
            var el = all[i];
            if (!el.offsetParent) continue;
            // leaf check — no meaningful child elements
            var childEls = el.querySelectorAll('*');
            var hasChildText = false;
            for (var c = 0; c < childEls.length; c++) {
                if ((childEls[c].innerText || childEls[c].textContent || '').trim().length > 0
                        && childEls[c].children.length === 0) {
                    hasChildText = true; break;
                }
            }
            var txt = (el.innerText || el.textContent || '').trim();
            if (txt !== label) continue;

            // Walk up looking for a container that has a control inside it
            var p = el.parentElement;
            for (var j = 0; j < maxDep; j++) {
                if (!p) break;
                // React-Select control
                var ctrl = p.querySelector('[class*="__control"],[class*="-control"]');
                if (ctrl && ctrl.offsetParent) return ctrl;
                // Plain <select>
                var sel = p.querySelector('select');
                if (sel && sel.offsetParent) return sel;
                p = p.parentElement;
            }
        }
        return null;
    """, label_text, max_depth)


# ============================================================
# CODING QUESTION RESTRICTION  (multi-select)
# ============================================================

def set_coding_question_restriction(driver, languages_str, progress_placeholder):
    progress_placeholder.info(f"  🖥️ Coding Question Restriction → '{languages_str}'")

    try:
        languages = [
            LANG_MAP.get(l.strip().lower(), l.strip())
            for l in str(languages_str).split(',')
            if l.strip() and l.strip().lower() not in ['nan', 'none', 'null', '']
        ]
        if not languages:
            progress_placeholder.info("  ⚙️ No languages to set — skipping")
            return True

        progress_placeholder.info(f"  🎯 Target languages: {languages}")
        ensure_tab_focus(driver)

        selected_count = 0
        for lang in languages:
            progress_placeholder.info(f"    → Selecting '{lang}'")
            lang_selected = False

            for attempt in range(6):
                # Always re-find the control each attempt — page may have re-rendered
                control = (
                    _find_element_by_label(driver, "Coding Question Restriction") or
                    _find_element_by_label(driver, "Select Coding Languages") or
                    driver.execute_script("""
                        // fallback: input with placeholder containing 'Coding'
                        var inp = document.querySelector(
                            'input[placeholder*="Coding"], input[placeholder*="coding"]');
                        if (inp && inp.offsetParent) return inp;
                        // fallback: first visible multi-value control
                        var ctrls = document.querySelectorAll('[class*="__control"],[class*="-control"]');
                        for (var i = 0; i < ctrls.length; i++) {
                            if (ctrls[i].offsetParent && ctrls[i].querySelector('[class*="multi"]'))
                                return ctrls[i];
                        }
                        return null;
                    """)
                )

                if not control:
                    progress_placeholder.warning(
                        f"      ⚠️ Control not found (attempt {attempt+1}) — retrying")
                    time.sleep(0.1)
                    continue

                # Open the dropdown and wait for options
                opened = _open_dropdown_by_element(driver, control)
                if not opened:
                    progress_placeholder.warning(
                        f"      ⚠️ Dropdown didn't open (attempt {attempt+1}) — retrying")
                    _dismiss_coding_overlays(driver)
                    time.sleep(0.1)
                    continue

                # Click the option
                if _click_visible_option(driver, lang):
                    progress_placeholder.info(f"      ✅ '{lang}' clicked (attempt {attempt+1})")
                    lang_selected = True
                    selected_count += 1
                    time.sleep(0.05)
                    break
                else:
                    progress_placeholder.warning(
                        f"      ⚠️ '{lang}' not in options (attempt {attempt+1}) — retrying")
                    _dismiss_coding_overlays(driver)
                    time.sleep(0.08)

            if not lang_selected:
                progress_placeholder.warning(f"    ❌ Could not select '{lang}' after 6 attempts")

        _dismiss_coding_overlays(driver)
        time.sleep(0.05)
        progress_placeholder.success(
            f"  ✅ Coding Question Restriction — {selected_count}/{len(languages)} selected")
        return selected_count == len(languages)

    except Exception as e:
        progress_placeholder.error(f"  ❌ set_coding_question_restriction error: {str(e)[:150]}")
        return False


# ============================================================
# DEFAULT CODING LANGUAGE  (single-select)
# ============================================================

def set_default_coding_language(driver, language, progress_placeholder):
    progress_placeholder.info(f"  🌐 Default Coding Language → '{language}'")

    try:
        lang = str(language).strip()
        if not lang or lang.lower() in ['nan', 'none', 'null', '']:
            progress_placeholder.info("  ⚙️ No Default Coding Language — skipping")
            return True

        normalized = LANG_MAP.get(lang.lower(), lang)
        progress_placeholder.info(f"  🎯 Target: '{normalized}'")
        ensure_tab_focus(driver)

        for attempt in range(6):
            # Re-find control every attempt
            control = (
                _find_element_by_label(driver, "Default Coding Language") or
                driver.execute_script("""
                    // fallback: first visible single-value react-select control
                    // that is NOT the multi-select restriction control
                    var ctrls = document.querySelectorAll('[class*="__control"],[class*="-control"]');
                    for (var i = 0; i < ctrls.length; i++) {
                        var c = ctrls[i];
                        if (!c.offsetParent) continue;
                        if (c.querySelector('[class*="multi"]')) continue;
                        return c;
                    }
                    return null;
                """)
            )

            if not control:
                progress_placeholder.warning(
                    f"    ⚠️ Control not found (attempt {attempt+1}) — retrying")
                time.sleep(0.1)
                continue

            # Open the dropdown
            opened = _open_dropdown_by_element(driver, control)
            if not opened:
                progress_placeholder.warning(
                    f"    ⚠️ Dropdown didn't open (attempt {attempt+1}) — retrying")
                _dismiss_coding_overlays(driver)
                time.sleep(0.1)
                continue

            # Click the option
            if _click_visible_option(driver, normalized):
                time.sleep(0.05)
                # Verify it actually set
                current = driver.execute_script("""
                    var c = arguments[0];
                    var sv = c.querySelector('[class*="single-value"]');
                    return sv ? (sv.innerText || sv.textContent || '').trim() : '';
                """, control)
                if normalized.lower() in (current or "").lower():
                    _dismiss_coding_overlays(driver)
                    progress_placeholder.success(
                        f"  ✅ Default Coding Language = '{current}' (attempt {attempt+1})")
                    return True
                else:
                    progress_placeholder.warning(
                        f"    ⚠️ Clicked but got '{current}' (attempt {attempt+1}) — retrying")
                    _dismiss_coding_overlays(driver)
                    time.sleep(0.08)
            else:
                progress_placeholder.warning(
                    f"    ⚠️ '{normalized}' not in options (attempt {attempt+1}) — retrying")
                _dismiss_coding_overlays(driver)
                time.sleep(0.08)

        # Final fallback — click_react_select
        progress_placeholder.warning("  ⚠️ Direct attempts exhausted — trying click_react_select fallback")
        if click_react_select(driver, "Default Coding Language", normalized, progress_placeholder):
            _dismiss_coding_overlays(driver)
            progress_placeholder.success(f"  ✅ Default Coding Language = '{normalized}' (fallback)")
            return True

        progress_placeholder.error(f"  ❌ Could not set Default Coding Language to '{normalized}'")
        return False

    except Exception as e:
        progress_placeholder.error(f"  ❌ set_default_coding_language error: {str(e)[:150]}")
        return False


# ============================================================
# SELECT SECTION TYPE
# ============================================================

def select_section_type(driver, wait, target_section, progress_placeholder):
    progress_placeholder.info(f"🎯 Selecting section type: '{target_section}'...")
    try:
        wait.until(EC.presence_of_element_located(
            (By.XPATH, "//*[contains(., 'Select Section Type')]")))
        progress_placeholder.info("  ✅ Section type modal loaded")
    except:
        progress_placeholder.warning("  ⚠️ Could not confirm modal — continuing")

    clicked = False
    for xp in [
        f"//*[normalize-space(text())='{target_section}']",
        f"//*[contains(text(),'{target_section}')]",
        f"//div[contains(@class,'card')]//h3[text()='{target_section}']",
        f"//button[contains(.,'{target_section}')]",
        f"//div[@role='button'][contains(.,'{target_section}')]",
    ]:
        for elem in [e for e in driver.find_elements(By.XPATH, xp) if e.is_displayed()]:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});"
                    "arguments[0].click();", elem)
                time.sleep(0.08)
                try:
                    modals = driver.find_elements(By.XPATH,
                        "//*[contains(normalize-space(.),'Select Section Type')]")
                    if not any(m.is_displayed() for m in modals):
                        clicked = True
                except:
                    clicked = True
                if clicked:
                    progress_placeholder.success(f"  ✅ '{target_section}' selected!")
                    break
            except:
                continue
        if clicked:
            break

    if not clicked:
        progress_placeholder.warning(
            f"⚠️ Could not auto-click '{target_section}'. Please click manually (10s)...")
        time.sleep(10)
    return clicked


# ============================================================
# FILL SECTION FORM
# ============================================================

def fill_section_form(driver, section_name, time_limit, progress_placeholder):
    progress_placeholder.info("📝 Filling section form...")

    if section_name:
        name_filled = False
        for xpath in [
            "//label[normalize-space(text())='Name of Section']/following-sibling::input[1]",
            "//label[normalize-space(text())='Name of section']/following-sibling::input[1]",
            "//label[contains(text(),'Name of Section')]/..//input[not(@type='number')]",
            "//*[normalize-space(text())='Name of Section']/following::input[not(@type='number')][1]",
        ]:
            try:
                field = driver.find_element(By.XPATH, xpath)
                if field.is_displayed() and (field.get_attribute("type") or "").lower() != "number":
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});"
                        "arguments[0].focus();", field)
                    _safe_send(driver, field, Keys.CONTROL + "a")
                    _safe_key(driver, field, 'DELETE')
                    _safe_send(driver, field, section_name)
                    driver.execute_script("""
                        arguments[0].dispatchEvent(new Event('input',  {bubbles: true}));
                        arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
                    """, field)
                    name_filled = True
                    progress_placeholder.success(f"  ✅ Section name: {section_name}")
                    break
            except:
                continue
        if not name_filled:
            progress_placeholder.warning("  ⚠️ Could not fill section name")

    if time_limit:
        time_filled = False
        progress_placeholder.info(f"  ⏰ Setting time limit to: {time_limit} mins")

        for xpath in [
            "//label[normalize-space(text())='Time Limit (in Mins)']/following-sibling::input[1]",
            "//label[contains(text(),'Time Limit (in Mins)')]/..//input[@type='number']",
            "//label[contains(text(),'Time Limit')]/..//input[@type='number']",
            "//*[normalize-space(text())='Time Limit (in Mins)']/following::input[@type='number'][1]",
        ]:
            try:
                field = driver.find_element(By.XPATH, xpath)
                if field.is_displayed():
                    current_value = field.get_attribute("value") or ""
                    progress_placeholder.info(f"    🔍 Current time limit value: '{current_value}'")
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});"
                        "arguments[0].focus();", field)
                    time.sleep(0.05)
                    _safe_send(driver, field, Keys.CONTROL + "a")
                    time.sleep(0.03)
                    _safe_key(driver, field, 'DELETE')
                    time.sleep(0.03)
                    driver.execute_script("""
                        var field = arguments[0];
                        field.value = '';
                        field.dispatchEvent(new Event('input', {bubbles: true}));
                        field.dispatchEvent(new Event('change', {bubbles: true}));
                    """, field)
                    time.sleep(0.03)
                    for _ in range(10):
                        _safe_key(driver, field, 'BACK_SPACE')
                        time.sleep(0.01)
                    cleared_value = field.get_attribute("value") or ""
                    progress_placeholder.info(f"    🧹 After clearing: '{cleared_value}'")
                    _safe_send(driver, field, str(time_limit))
                    time.sleep(0.05)
                    driver.execute_script("""
                        var field = arguments[0];
                        field.dispatchEvent(new Event('input',  {bubbles: true}));
                        field.dispatchEvent(new Event('change', {bubbles: true}));
                        field.dispatchEvent(new Event('blur',   {bubbles: true}));
                    """, field)
                    final_value = field.get_attribute("value") or ""
                    progress_placeholder.info(f"    ✅ Final time limit value: '{final_value}'")
                    if final_value == str(time_limit):
                        time_filled = True
                        progress_placeholder.success(
                            f"  ✅ Time limit successfully set: {time_limit} mins")
                        break
                    else:
                        progress_placeholder.warning(
                            f"    ⚠️ Value mismatch: expected '{time_limit}', got '{final_value}'")
            except Exception as e:
                progress_placeholder.warning(
                    f"    ⚠️ Error with xpath {xpath}: {str(e)[:50]}")
                continue

        if not time_filled:
            progress_placeholder.warning("  ❌ Could not fill time limit after trying all methods")

    progress_placeholder.success("  ✅ Section form filled — ready to add questions")


# ============================================================
# HANDLE SUBJECT SELECTION
# ============================================================

def handle_subject_selection(driver, progress_placeholder):
    try:
        els = driver.find_elements(By.XPATH,
            "//*[contains(text(),'Select Subject') or "
            "contains(text(),'Choose Subject') or "
            "contains(text(),'Subject')]")
        if not els:
            return
        progress_placeholder.info("📚 Subject selection detected — clicking first subject...")
        for xpath in [
            "//div[contains(@class,'subject') or contains(@class,'category')]//button",
            "//div[contains(@class,'card')]",
            "//*[contains(@role,'button')]",
        ]:
            for subj in driver.find_elements(By.XPATH, xpath)[:3]:
                try:
                    if subj.is_displayed():
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block:'center'});"
                            "arguments[0].click();", subj)
                        progress_placeholder.success("  ✅ Subject selected!")
                        time.sleep(0.03)
                        return
                except:
                    continue
    except:
        pass


# ============================================================
# CLOSE THE ADD QUESTIONS POPUP
# ============================================================

def close_add_questions_popup(driver, progress_placeholder):
    ensure_tab_focus(driver)
    progress_placeholder.info("🔲 Closing the Add Questions popup...")
    popup_closed = False

    for attempt in range(5):
        try:
            close_btn = driver.find_element(
                By.CSS_SELECTOR, '[data-testid="aqp-close-icon"]')
            if close_btn and close_btn.is_displayed():
                driver.execute_script("arguments[0].click();", close_btn)
                still_open = not poll_popup_closed(driver, timeout=2.0)
                if not still_open:
                    progress_placeholder.success("  ✅ Popup closed via close button!")
                    popup_closed = True
                    break
                else:
                    progress_placeholder.warning(
                        f"  ⚠️ Popup still visible after close attempt {attempt+1}")
        except Exception as e:
            progress_placeholder.warning(
                f"  ⚠️ Close button attempt {attempt+1}: {str(e)[:60]}")
        poll_popup_closed(driver, timeout=0.3)

    if not popup_closed:
        try:
            driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
            still_open = not poll_popup_closed(driver, timeout=2.0)
            if not still_open:
                progress_placeholder.success("  ✅ Popup closed via Escape key!")
                popup_closed = True
        except Exception as e:
            progress_placeholder.warning(f"  ⚠️ Escape key failed: {str(e)[:60]}")

    if not popup_closed:
        try:
            driver.execute_script("""
                ['[role="dialog"]', '.modal', '[data-testid="aqp-close-icon"]'].forEach(
                    function(sel) {
                        document.querySelectorAll(sel).forEach(function(el) {
                            el.style.display = 'none';
                        });
                    });
                document.body.style.overflow = 'auto';
                document.body.classList.remove('modal-open', 'no-scroll');
            """)
            poll_popup_closed(driver, timeout=0.3)
            progress_placeholder.success("  ✅ Popup hidden via JS fallback")
            popup_closed = True
        except Exception as e:
            progress_placeholder.error(f"  ❌ JS popup hide failed: {str(e)[:60]}")

    return popup_closed


# ============================================================
# CLICK "ADD SECTION →" BUTTON
# ============================================================

def click_add_section_button(driver, progress_placeholder):
    ensure_tab_focus(driver)
    progress_placeholder.info("📌 Looking for 'Add Section →' button...")

    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    poll_button_visible(driver, "Add Section", timeout=1.5)

    for attempt in range(15):
        progress_placeholder.info(f"  🔍 Attempt {attempt+1}/15")
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.08)

        btn = driver.execute_script("""
            var b = document.querySelector('button[data-testid="cnsf-footer-cta-button"]');
            if (b && b.offsetParent) {
                if ((b.innerText || b.textContent || '').includes('Add Section')) return b;
            }
            return null;
        """)

        if not btn:
            btn = driver.execute_script("""
                var buttons = document.querySelectorAll('button');
                for (var i = 0; i < buttons.length; i++) {
                    var b = buttons[i];
                    if ((b.innerText || b.textContent || '').trim().includes('Add Section')
                            && b.offsetParent) return b;
                }
                return null;
            """)

        if btn:
            btn_text = driver.execute_script(
                "return (arguments[0].innerText || arguments[0].textContent || '').trim();", btn)
            progress_placeholder.info(f"  🎯 Found: '{btn_text}'")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.05)
            try:
                from selenium.webdriver.common.action_chains import ActionChains
                ActionChains(driver).move_to_element(btn).pause(0.03).click().perform()
                progress_placeholder.info("  ✅ ActionChains click on Add Section")
            except:
                driver.execute_script("arguments[0].click();", btn)
                progress_placeholder.info("  ✅ JS click on Add Section")
            _old_url = driver.current_url
            poll_url_changed(driver, _old_url, timeout=2.0)
            progress_placeholder.success("  🎉 'Add Section →' clicked!")
            return True
        else:
            progress_placeholder.warning(
                f"  ⚠️ Add Section button not found (attempt {attempt+1})")
            if attempt % 5 == 4:
                visible_btns = driver.execute_script("""
                    var r = [];
                    document.querySelectorAll('button').forEach(function(b) {
                        if (b.offsetParent)
                            r.push((b.innerText||b.textContent||'').trim().slice(0,40));
                    });
                    return r;
                """)
                progress_placeholder.info(f"  🔍 Visible buttons: {visible_btns}")
            poll_button_visible(driver, "Add Section", timeout=0.7)

    progress_placeholder.error("  ❌ Could not find 'Add Section →' button after 15 attempts")
    return False


# ============================================================
# CLICK "CREATE NEW SECTION" BUTTON
# ============================================================

def click_create_new_section_button(driver, wait, progress_placeholder):
    progress_placeholder.info("➕ Looking for 'Create New Section' button...")
    poll_button_visible(driver, "Section", timeout=2.0)

    for attempt in range(15):
        progress_placeholder.info(f"  🔍 Attempt {attempt+1}/15")
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.08)

        btn = driver.execute_script("""
            var buttons = document.querySelectorAll('button, a, div[role="button"]');
            for (var i = 0; i < buttons.length; i++) {
                var b = buttons[i];
                var txt = (b.innerText || b.textContent || '').trim();
                if ((txt.includes('Create') && txt.includes('Section')) && b.offsetParent)
                    return b;
                if (txt.includes('Create new Section') && b.offsetParent)
                    return b;
                if (txt.includes('Add New Section') && b.offsetParent)
                    return b;
            }
            return null;
        """)

        if not btn:
            for xp in [
                "//*[contains(text(),'Create new Section') or contains(text(),'Create New Section')]",
                "//*[contains(text(),'Add New Section')]",
                "//button[contains(.,'Section')]",
            ]:
                try:
                    els = [e for e in driver.find_elements(By.XPATH, xp) if e.is_displayed()]
                    if els:
                        btn = els[0]
                        break
                except:
                    continue

        if btn:
            btn_text = driver.execute_script(
                "return (arguments[0].innerText || arguments[0].textContent || '').trim();", btn)
            progress_placeholder.info(f"  🎯 Found: '{btn_text}'")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.05)
            try:
                from selenium.webdriver.common.action_chains import ActionChains
                ActionChains(driver).move_to_element(btn).pause(0.03).click().perform()
                progress_placeholder.info("  ✅ ActionChains click on Create New Section")
            except:
                driver.execute_script("arguments[0].click();", btn)
                progress_placeholder.info("  ✅ JS click on Create New Section")
            poll_element_visible(driver, "//*[contains(.,'Select Section Type')]", timeout=2.0)
            progress_placeholder.success("  🎉 'Create New Section' clicked!")
            return True
        else:
            progress_placeholder.warning(
                f"  ⚠️ Create New Section button not found (attempt {attempt+1})")
            if attempt % 5 == 4:
                visible_btns = driver.execute_script("""
                    var r = [];
                    document.querySelectorAll('button').forEach(function(b) {
                        if (b.offsetParent)
                            r.push((b.innerText||b.textContent||'').trim().slice(0,40));
                    });
                    return r;
                """)
                progress_placeholder.info(f"  🔍 Visible buttons: {visible_btns}")
            poll_button_visible(driver, "Section", timeout=0.7)

    progress_placeholder.error("  ❌ Could not find 'Create New Section' button")
    return False


# ============================================================
# CLICK "SAVE & NEXT" BUTTON
# ============================================================

def click_save_and_next_button(driver, progress_placeholder):
    ensure_tab_focus(driver)
    progress_placeholder.info("📌 Looking for 'Save & Next' button...")
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    poll_button_visible(driver, "Save", timeout=2.0)

    for attempt in range(10):
        progress_placeholder.info(f"  🔍 Attempt {attempt+1}/10")
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.08)

        btn = driver.execute_script("""
            var buttons = document.querySelectorAll('button');
            for (var i = 0; i < buttons.length; i++) {
                var b = buttons[i];
                var txt = (b.innerText || b.textContent || '').trim();
                if (txt.includes('Save') && txt.includes('Next') && b.offsetParent) return b;
            }
            return null;
        """)

        if btn:
            btn_text = driver.execute_script(
                "return (arguments[0].innerText || arguments[0].textContent || '').trim();", btn)
            progress_placeholder.info(f"  🎯 Found: '{btn_text}'")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.05)
            try:
                from selenium.webdriver.common.action_chains import ActionChains
                ActionChains(driver).move_to_element(btn).pause(0.03).click().perform()
                progress_placeholder.info("  ✅ ActionChains click on Save & Next")
            except:
                driver.execute_script("arguments[0].click();", btn)
                progress_placeholder.info("  ✅ JS click on Save & Next")
            _old_url = driver.current_url
            poll_url_changed(driver, _old_url, timeout=3.0)
            progress_placeholder.success("  🎉 'Save & Next' clicked!")
            return True
        else:
            progress_placeholder.warning(
                f"  ⚠️ Save & Next button not found (attempt {attempt+1})")
            if attempt % 3 == 2:
                visible_btns = driver.execute_script("""
                    var r = [];
                    document.querySelectorAll('button').forEach(function(b) {
                        if (b.offsetParent)
                            r.push((b.innerText||b.textContent||'').trim().slice(0,40));
                    });
                    return r;
                """)
                progress_placeholder.info(f"  🔍 Visible buttons: {visible_btns}")
            poll_button_visible(driver, "Save", timeout=0.7)

    progress_placeholder.error("  ❌ Could not find or click 'Save & Next' button")
    return False


# ============================================================
# QUESTION LOOP
# ============================================================

def process_section_questions(driver, questions_df, section_num, progress_placeholder,
                              section_type=""):
    total = len(questions_df)
    progress_placeholder.info(f"🚀 Section {section_num}: Processing {total} question rows")

    needs_marks = section_type.strip().lower() in MARKS_SECTION_TYPES
    if needs_marks:
        progress_placeholder.info(
            f"  📝 Section type '{section_type}' → Marks for Each Question will be filled")
    else:
        progress_placeholder.info(
            f"  📝 Section type '{section_type}' → Marks for Each Question skipped")

    def is_empty(val):
        return str(val).strip().lower() in ["nan", "none", "null", ""]

    def wait_popup_open(secs=10):
        for _ in range(secs * 20):
            try:
                els = driver.find_elements(By.XPATH,
                    "//h2[normalize-space(text())='Add Questions'] | "
                    "//h2[normalize-space(text())='Edit Questions'] | "
                    "//*[@role='dialog']//*[normalize-space(text())='Add Questions'] | "
                    "//*[@role='dialog']//*[normalize-space(text())='Edit Questions']")
                if any(e.is_displayed() for e in els):
                    return True
                els2 = driver.find_elements(By.XPATH,
                    "//*[normalize-space(text())='Question Library']")
                if any(e.is_displayed() for e in els2):
                    return True
            except:
                pass
            time.sleep(0.05)
        return False

    def wait_popup_closed(secs=15):
        for _ in range(secs * 20):
            try:
                els = driver.find_elements(By.XPATH,
                    "//*[normalize-space(text())='Question Library']")
                if not any(e.is_displayed() for e in els):
                    return True
            except:
                pass
            time.sleep(0.05)
        return False

    def click_page_open_button():
        for _ in range(40):
            try:
                clicked = driver.execute_script("""
                    var btns = document.querySelectorAll('button, a');
                    for (var i = 0; i < btns.length; i++) {
                        var el  = btns[i];
                        var txt = (el.innerText || el.textContent || '').trim();
                        if (txt.indexOf('Add')      === -1) continue;
                        if (txt.indexOf('Question') === -1) continue;
                        if (txt.indexOf('\u2192')   !== -1) continue;
                        if (txt.indexOf('->')       !== -1) continue;
                        if (!el.offsetParent)               continue;
                        var r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0)  continue;
                        el.scrollIntoView({block: 'center'});
                        el.click();
                        return txt;
                    }
                    return null;
                """)
                if clicked:
                    progress_placeholder.info(f"    ✅ Clicked: '{clicked}'")
                    return True
            except:
                pass
            time.sleep(0.08)
        return False

    failed_rows = []

    for idx, row in questions_df.iterrows():
        row_num = idx + 1
        progress_placeholder.info(
            f"\n{'='*55}\n  SECTION {section_num} — ROW {row_num} / {total}\n{'='*55}")

        qlib  = str(row.get("Question Library",  "Topin Questions")).strip()
        topic = str(row.get("Topic",             "")).strip()
        diff  = str(row.get("Difficulty Level",  "")).strip()
        sub   = str(row.get("Sub Topic",         "")).strip()

        all_tags = []
        exclusive_tag_columns = [
            col for col in questions_df.columns if col.startswith("Exclusive Tags")]
        if exclusive_tag_columns:
            progress_placeholder.info(
                f"  🔍 Found {len(exclusive_tag_columns)} exclusive tag columns: "
                f"{exclusive_tag_columns}")
        for col in exclusive_tag_columns:
            tag_value = str(row.get(col, "")).strip()
            if not is_empty(tag_value):
                all_tags.append(tag_value)
                progress_placeholder.info(f"    📝 {col}: '{tag_value}'")

        tags  = ", ".join(all_tags) if all_tags else ""
        num_q = str(row.get("Number of Questions", "")).strip()
        marks = ""
        if needs_marks:
            marks = str(row.get("Marks for Each Question", "")).strip()

        if not is_empty(diff):
            diff = diff.capitalize()

        progress_placeholder.info(
            f"  📋 QL={qlib} | Topic={topic} | Diff={diff} | Sub={sub} "
            f"| Tags={tags} | Num={num_q}"
            + (f" | Marks={marks}" if needs_marks else ""))

        wait_popup_closed(secs=2)

        if not click_page_open_button():
            reason = "Could not open popup"
            progress_placeholder.error(f"  ❌ Row {row_num} FAILED — {reason}")
            failed_rows.append({"Section": section_num, "Row": row_num, "Topic": topic,
                                 "Difficulty": diff, "Sub Topic": sub, "Num Q": num_q,
                                 "Marks": marks, "Reason": reason})
            continue

        if not wait_popup_open(secs=3):
            reason = "Popup did not open"
            progress_placeholder.error(f"  ❌ Row {row_num} FAILED — {reason}")
            failed_rows.append({"Section": section_num, "Row": row_num, "Topic": topic,
                                 "Difficulty": diff, "Sub Topic": sub, "Num Q": num_q,
                                 "Marks": marks, "Reason": reason})
            continue

        time.sleep(0.02)
        ensure_tab_focus(driver)

        handle_question_library(driver, qlib, progress_placeholder)

        if not is_empty(topic):
            click_react_select(driver, "Topic", topic, progress_placeholder)
        if not is_empty(diff):
            click_react_select(driver, "Difficulty Level", diff, progress_placeholder)
        if not is_empty(sub):
            click_react_select(driver, "Sub Topic", sub, progress_placeholder)

        # -------------------------------------------------------
        # FIX: After exclusive tags, aggressively dismiss any open
        # tag suggestion dropdowns / overlays before proceeding to
        # the "Add Questions" button. This eliminates the 2% failure
        # where an invisible overlay intercepts the button click.
        # -------------------------------------------------------
        if not is_empty(tags):
            set_exclusive_tags(driver, tags, progress_placeholder)
            # Immediately dismiss overlays that could block "Add Questions"
            dismiss_tag_overlays(driver, progress_placeholder)
        # -------------------------------------------------------

        try:
            driver.execute_script(
                "if(document.activeElement) document.activeElement.blur();")
        except:
            pass

        if not is_empty(num_q):
            set_number_of_questions(driver, num_q, progress_placeholder)
            time.sleep(0.01)

        if needs_marks and not is_empty(marks):
            set_marks_per_question(driver, marks, progress_placeholder)
            time.sleep(0.01)

        progress_placeholder.info("  📤 Clicking 'Add Questions →'...")
        submitted = False

        for submit_attempt in range(8):
            progress_placeholder.info(f"    🔁 Attempt {submit_attempt + 1}/8")
            time.sleep(0.08)

            btn = None
            btn_info = {}
            try:
                all_btns = driver.find_elements(By.XPATH,
                    "//button[contains(., 'Add Questions') "
                    "and not(starts-with(normalize-space(.), '+'))]")
                best_bottom = -1
                for b in all_btns:
                    try:
                        rect = driver.execute_script(
                            "var r=arguments[0].getBoundingClientRect();"
                            "return {bottom:r.bottom,width:r.width,height:r.height,"
                            "disabled:arguments[0].disabled};", b)
                        if rect['width'] > 0 and rect['bottom'] > best_bottom:
                            best_bottom = rect['bottom']
                            btn = b
                            btn_info = rect
                    except:
                        pass
            except:
                pass

            if not btn:
                progress_placeholder.warning(
                    f"    ⚠️ Button not found on attempt {submit_attempt+1}")
                time.sleep(0.02)
                continue

            btn_txt = (btn.text or "").strip()
            progress_placeholder.info(
                f"    🎯 Found: '{btn_txt}' | disabled={btn_info.get('disabled')}")

            if btn_info.get('disabled'):
                reason = "No questions available — button disabled"
                progress_placeholder.warning(f"  ⚠️ Row {row_num} SKIPPED — {reason}")
                failed_rows.append({"Section": section_num, "Row": row_num, "Topic": topic,
                                     "Difficulty": diff, "Sub Topic": sub, "Num Q": num_q,
                                     "Marks": marks, "Reason": reason})
                submitted = True
                break

            # FIX: Before each click attempt, dismiss any lingering overlays
            # that could intercept the button click (tag suggestions, etc.)
            try:
                driver.execute_script("""
                    if (document.activeElement) document.activeElement.blur();
                    var overlaySelectors = [
                        '[class*="suggestion"]',
                        '[class*="autocomplete"]',
                        '[class*="tag-suggest"]',
                        '[class*="Tag__menu"]',
                        '[class*="tags__menu"]',
                        '[class*="TagInput__dropdown"]',
                        '[class*="tag-dropdown"]',
                        '[class*="__dropdown"]',
                        '[class*="-dropdown"]',
                        '[class*="__menu"]',
                        '[class*="-menu"]'
                    ];
                    for (var o = 0; o < overlaySelectors.length; o++) {
                        var overlays = document.querySelectorAll(overlaySelectors[o]);
                        for (var j = 0; j < overlays.length; j++) {
                            if (overlays[j].offsetParent) {
                                overlays[j].style.display = 'none';
                                overlays[j].style.visibility = 'hidden';
                                overlays[j].style.pointerEvents = 'none';
                            }
                        }
                    }
                """)
            except:
                pass

            try:
                from selenium.webdriver.common.action_chains import ActionChains
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.02)
                ActionChains(driver).move_to_element(btn).pause(0.03).click().perform()
                progress_placeholder.info("    ✅ ActionChains click performed!")
            except Exception as e:
                progress_placeholder.warning(
                    f"    ⚠️ ActionChains failed: {str(e)[:60]}, trying JS...")
                try:
                    driver.execute_script("arguments[0].click();", btn)
                    progress_placeholder.info("    ✅ JS click performed!")
                except Exception as e2:
                    # FIX: Final fallback — coordinate-based click that bypasses
                    # any overlay by clicking whatever element is actually at those
                    # screen coordinates, then also firing directly on the button.
                    progress_placeholder.warning(
                        f"    ⚠️ Both clicks failed: {str(e2)[:60]} — trying coordinate click")
                    try:
                        driver.execute_script("""
                            var btn = arguments[0];
                            var r = btn.getBoundingClientRect();
                            var cx = r.left + r.width  / 2;
                            var cy = r.top  + r.height / 2;

                            // Click whatever element is visually on top at those coords
                            var topEl = document.elementFromPoint(cx, cy);
                            if (topEl) {
                                topEl.dispatchEvent(new MouseEvent('mousedown',
                                    {bubbles:true, cancelable:true, clientX:cx, clientY:cy}));
                                topEl.dispatchEvent(new MouseEvent('mouseup',
                                    {bubbles:true, cancelable:true, clientX:cx, clientY:cy}));
                                topEl.dispatchEvent(new MouseEvent('click',
                                    {bubbles:true, cancelable:true, clientX:cx, clientY:cy}));
                            }

                            // Also fire directly on the button regardless of overlay
                            btn.dispatchEvent(new MouseEvent('mousedown',
                                {bubbles:true, cancelable:true}));
                            btn.dispatchEvent(new MouseEvent('mouseup',
                                {bubbles:true, cancelable:true}));
                            btn.dispatchEvent(new MouseEvent('click',
                                {bubbles:true, cancelable:true}));
                        """, btn)
                        progress_placeholder.info("    ✅ Coordinate + direct JS click performed!")
                    except:
                        pass
                    time.sleep(0.05)
                    continue

            poll_popup_closed(driver, timeout=0.2)
            submitted = True
            progress_placeholder.success(
                f"  ✅ Section {section_num} Row {row_num}/{total} submitted!")
            break

        if not submitted:
            reason = "'Add Questions →' not clicked after 8 attempts"
            progress_placeholder.error(f"  ❌ Row {row_num} FAILED — {reason}")
            failed_rows.append({"Section": section_num, "Row": row_num, "Topic": topic,
                                 "Difficulty": diff, "Sub Topic": sub, "Num Q": num_q,
                                 "Marks": marks, "Reason": reason})
            time.sleep(0.02)

    done = total - len(failed_rows)
    progress_placeholder.success(
        f"✅ Section {section_num} questions done — {done}/{total} added")
    return done, failed_rows


# ============================================================
# MASTER MULTI-SECTION ORCHESTRATOR
# ============================================================

def automate_all_sections(driver, wait, sections, progress_placeholder):
    total_sections  = len(sections)
    all_failed_rows = []

    for sec_idx, section in enumerate(sections):
        sec_num             = sec_idx + 1
        sec_type            = section["section_type"]
        sec_name            = section["section_name"]
        sec_time            = section["time_limit"]
        coding_restriction  = section.get("coding_restriction",  "")
        default_coding_lang = section.get("default_coding_lang", "")
        questions_df        = section["questions_df"]
        is_last             = (sec_idx == total_sections - 1)
        coding_section_types = {"coding", "ide based coding", "sql", "sql coding", "web coding"}
        is_coding_section   = sec_type.strip().lower() in coding_section_types

        progress_placeholder.info(
            f"\n{'#'*60}\n"
            f"  SECTION {sec_num} / {total_sections} — {sec_type}\n"
            f"  Coding Section: {is_coding_section}\n"
            f"  Coding Restriction: '{coding_restriction}'\n"
            f"  Default Coding Lang: '{default_coding_lang}'\n"
            f"{'#'*60}")

        if sec_idx > 0:
            progress_placeholder.info(
                f"🎯 Selecting type for Section {sec_num}: '{sec_type}'")
            select_section_type(driver, wait, sec_type, progress_placeholder)
            poll_element_visible(driver, "//label[contains(text(),'Name')]", timeout=1.0)

        fill_section_form(driver, sec_name, sec_time, progress_placeholder)

        if not questions_df.empty:
            done, failed = process_section_questions(
                driver, questions_df, sec_num, progress_placeholder,
                section_type=sec_type)
            all_failed_rows.extend(failed)
        else:
            progress_placeholder.warning(
                f"  ⚠️ Section {sec_num} has no question rows — skipping question loop")

        if not close_add_questions_popup(driver, progress_placeholder):
            progress_placeholder.error(
                f"  ❌ Could not close popup after Section {sec_num} — aborting")
            return False

        poll_element_visible(driver, "//*[contains(text(),'Coding') or contains(text(),'Save')]", timeout=0.5)

        coding_keywords = ["coding", "ide", "sql", "web coding"]
        has_coding_keyword = any(keyword in sec_type.lower() for keyword in coding_keywords)
        has_coding_data = bool(coding_restriction) or bool(default_coding_lang)
        should_process_coding = is_coding_section or has_coding_keyword or has_coding_data

        if sec_type.strip().lower() in ["coding", "web coding", "ide based coding", "sql", "sql coding"]:
            should_process_coding = True
        if coding_restriction or default_coding_lang:
            should_process_coding = True

        if should_process_coding:
            progress_placeholder.info(f"🚀 EXECUTING CODING SECTION SETUP FOR SECTION {sec_num}")
            progress_placeholder.info(f"  📋 CSV values — Restriction: '{coding_restriction}' | Default Lang: '{default_coding_lang}'")
            ensure_tab_focus(driver)

            # Only set Coding Question Restriction if CSV has a value — no fallback
            if coding_restriction and coding_restriction.strip().lower() not in ['nan', 'none', 'null', '']:
                try:
                    result1 = set_coding_question_restriction(driver, coding_restriction, progress_placeholder)
                    if result1:
                        progress_placeholder.success("    ✅ Coding Question Restriction completed")
                    else:
                        progress_placeholder.error("    ❌ Coding Question Restriction failed")
                except Exception as e:
                    progress_placeholder.error(f"    ❌ Exception: {str(e)[:100]}")
            else:
                progress_placeholder.info("    ⚙️ Coding Question Restriction: not set in CSV — skipping")

            # Only set Default Coding Language if CSV has a value — no fallback
            if default_coding_lang and default_coding_lang.strip().lower() not in ['nan', 'none', 'null', '']:
                try:
                    result2 = set_default_coding_language(driver, default_coding_lang, progress_placeholder)
                    if result2:
                        progress_placeholder.success("    ✅ Default Coding Language completed")
                    else:
                        progress_placeholder.error("    ❌ Default Coding Language failed")
                except Exception as e:
                    progress_placeholder.error(f"    ❌ Exception: {str(e)[:100]}")
            else:
                progress_placeholder.info("    ⚙️ Default Coding Language: not set in CSV — skipping")

            progress_placeholder.success(f"🎉 CODING SECTION SETUP COMPLETED FOR SECTION {sec_num}")
        else:
            progress_placeholder.info(f"  ℹ️ Section type '{sec_type}' is not coding-related — skipping")

        if not click_add_section_button(driver, progress_placeholder):
            progress_placeholder.error(
                f"  ❌ Could not click 'Add Section →' for Section {sec_num} — aborting")
            return False

        if is_last:
            progress_placeholder.info(
                f"🏁 Section {sec_num} is the LAST section — clicking 'Save & Next'")
            if not click_save_and_next_button(driver, progress_placeholder):
                return False

            progress_placeholder.info("🔗 Capturing final URL...")
            _old = driver.current_url
            poll_url_changed(driver, _old, timeout=2.0)
            try:
                final_url = driver.current_url
                progress_placeholder.success(f"  ✅ Final URL: {final_url}")

                st.divider()
                st.success(
                    f"🎉 Assessment with {total_sections} section(s) created successfully!")
                st.markdown("### 🔗 Final Assessment Page URL")
                st.code(final_url, language=None)
                st.markdown(
                    f'<a href="{final_url}" target="_blank" '
                    f'style="font-size:18px; font-weight:bold; color:#1f77b4;">'
                    f'🌐 Open Final Assessment Page ↗</a>',
                    unsafe_allow_html=True)

                if all_failed_rows:
                    st.warning("⚠️ Some rows were skipped/failed:")
                    st.dataframe(pd.DataFrame(all_failed_rows), use_container_width=True)

                st.markdown("### ✅ Automation Summary")
                summary_lines = [
                    f"✅ {total_sections} section(s) processed",
                    f"✅ Popup closed after each section",
                    f"✅ 'Add Section →' clicked for each section",
                    f"✅ 'Save & Next' clicked on final section",
                    f"✅ Final URL captured",
                ]
                st.info("\n\n".join(summary_lines))
                return True

            except Exception as e:
                progress_placeholder.error(f"  ❌ Could not capture URL: {str(e)[:100]}")
                st.error("⚠️ Assessment may have been created but URL capture failed")
                return False

        else:
            progress_placeholder.info(
                f"➡️ Section {sec_num} done — creating Section {sec_num + 1}...")
            if not click_create_new_section_button(driver, wait, progress_placeholder):
                progress_placeholder.error(
                    f"  ❌ Could not click 'Create New Section' after Section "
                    f"{sec_num} — aborting")
                return False
            poll_element_visible(driver, "//label[contains(text(),'Name')]", timeout=1.0)

    return True


# ============================================================
# RUN AUTOMATION
# ============================================================

def run_automation(mobile_num, otp_code, sections, wait_time=10):
    start_time           = time.time()
    driver               = None
    progress_placeholder = st.empty()

    try:
        progress_placeholder.info("🔧 Initializing browser...")

        options = Options()
        options.page_load_strategy = 'normal'
        options.add_argument('--start-maximized')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-background-timer-throttling')
        options.add_argument('--disable-backgrounding-occluded-windows')
        options.add_argument('--disable-renderer-backgrounding')
        options.add_experimental_option("prefs", {
            "profile.default_content_setting_values.notifications": 2,
        })

        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options
        )
        driver.set_page_load_timeout(60)
        driver.implicitly_wait(2)
        wait = WebDriverWait(driver, wait_time)

        driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': """
                document.__proto__.hasFocus = function(){ return true; };
                document.hasFocus           = function(){ return true; };
                try {
                    Object.defineProperty(document, 'visibilityState', {
                        get: function(){ return 'visible'; }, configurable: true
                    });
                    Object.defineProperty(document, 'hidden', {
                        get: function(){ return false; }, configurable: true
                    });
                } catch(e) {}
                window.addEventListener('visibilitychange', function(e){
                    e.stopImmediatePropagation();
                }, true);
                window.addEventListener('blur', function(e){
                    e.stopImmediatePropagation();
                }, true);
            """
        })

        progress_placeholder.info("🔐 Step 1: Loading login page...")
        try:
            driver.get("https://config.topin.tech")
        except TimeoutException:
            progress_placeholder.warning("⚠️ Page load slow, continuing...")
            try:
                driver.execute_script("window.stop();")
            except:
                pass

        mobile_input = WebDriverWait(driver, 5).until(EC.presence_of_element_located(
            (By.XPATH, "//input[contains(@placeholder,'Number')]")))
        time.sleep(0.02)
        driver.execute_script("""
            var el = arguments[0], val = arguments[1];
            el.focus();
            var s = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            s.call(el, val);
            el.dispatchEvent(new Event('input',  {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        """, mobile_input, mobile_num)
        time.sleep(0.02)

        find_and_click(driver, "//button[contains(., 'GET OTP')]", timeout=wait_time)

        progress_placeholder.info("🔢 Step 2: Entering OTP...")
        wait.until(EC.presence_of_element_located(
            (By.XPATH, "//button[contains(., 'Verify')]")))

        filled = driver.execute_script("""
            var otp = arguments[0], filled = 0;
            var boxes = document.querySelectorAll('input[maxlength="1"]');
            if (boxes.length >= 6) {
                for (var i = 0; i < 6; i++) {
                    var s = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    s.call(boxes[i], otp[i]);
                    boxes[i].dispatchEvent(new Event('input',  {bubbles:true}));
                    boxes[i].dispatchEvent(new Event('change', {bubbles:true}));
                    boxes[i].dispatchEvent(
                        new KeyboardEvent('keyup', {key:otp[i],bubbles:true}));
                    filled++;
                }
                return filled;
            }
            var sels = ['input[type="text"]','input[type="tel"]','input[type="number"]'];
            for (var s2 = 0; s2 < sels.length && filled < 6; s2++) {
                var inputs = document.querySelectorAll(sels[s2]);
                for (var j = 0; j < inputs.length && filled < otp.length; j++) {
                    if (!inputs[j].offsetParent) continue;
                    var setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    setter.call(inputs[j], otp[filled]);
                    inputs[j].dispatchEvent(new Event('input',  {bubbles:true}));
                    inputs[j].dispatchEvent(new Event('change', {bubbles:true}));
                    inputs[j].dispatchEvent(
                        new KeyboardEvent('keyup',{key:otp[filled],bubbles:true}));
                    filled++;
                }
            }
            return filled;
        """, otp_code)

        if filled >= 6:
            progress_placeholder.success("  ✅ OTP entered")
        else:
            progress_placeholder.warning(
                f"  ⚠️ Only {filled}/6 OTP digits filled — please complete manually")

        time.sleep(0.02)
        find_and_click(driver, "//button[contains(., 'Verify')]", timeout=wait_time)
        progress_placeholder.success("✅ Login Successful!")

        progress_placeholder.info("🚀 Step 3: Navigating to Create Assessment...")
        find_and_click(
            driver, "//*[contains(text(), 'Create Assessment')]", timeout=wait_time)
        find_and_click(
            driver, "//*[contains(text(), 'Custom Assessment')]", timeout=wait_time)

        progress_placeholder.info("➕ Step 4: Creating Section 1...")
        find_and_click(driver,
            "//*[contains(text(),'Create new Section') or "
            "contains(text(),'Create New Section')]",
            timeout=wait_time)

        handle_subject_selection(driver, progress_placeholder)

        sec1_type = sections[0]["section_type"]
        progress_placeholder.info(f"🎯 Selecting type for Section 1: '{sec1_type}'")
        select_section_type(driver, wait, sec1_type, progress_placeholder)
        poll_element_visible(driver, "//label[contains(text(),'Name')]", timeout=1.0)

        progress_placeholder.success(
            f"✅ Setup done — handing off to multi-section orchestrator "
            f"({len(sections)} section(s))")

        success = automate_all_sections(driver, wait, sections, progress_placeholder)

        elapsed = time.time() - start_time
        if success:
            progress_placeholder.success(
                f"🎉 Full automation complete! Time: {elapsed:.1f}s")
            st.balloons()
        else:
            progress_placeholder.error("❌ Automation ended with errors")

    except TimeoutException:
        progress_placeholder.error("⏱️ Timeout — try increasing wait time")
    except WebDriverException as e:
        progress_placeholder.error(f"🌐 Browser error: {str(e)[:100]}")
    except Exception as e:
        progress_placeholder.error(f"❌ Error: {str(e)[:150]}")
    finally:
        try:
            driver.quit()
        except:
            pass


# ============================================================
# STREAMLIT TRIGGER BUTTON
# ============================================================

st.write("---")
if st.button("🚀 START AUTOMATION", use_container_width=True, type="primary"):
    if not uploaded_file or not mob or len(otp_val) != 6:
        st.warning(
            "⚠️ Please provide all required fields (CSV, mobile number, 6-digit OTP)")
    else:
        try:
            raw_df = pd.read_csv(uploaded_file, header=None)

            st.write("---")
            st.subheader("📊 CSV Preview")
            total_rows   = len(raw_df)
            preview_rows = min(100, total_rows)
            st.caption(
                f"Total rows: **{total_rows}** | Showing first **{preview_rows}**")
            st.dataframe(raw_df.head(preview_rows), use_container_width=True)
            st.write("---")

            sections = parse_sections_from_csv(raw_df)

            if not sections:
                st.error(
                    "❌ No valid sections found in CSV. Check the format guide above.")
            else:
                st.success(f"✅ Found **{len(sections)}** section(s) in CSV")

                valid_section_types = [
                    "MCQ", "Coding", "SQL", "Textual", "Web Coding",
                    "Fill in the Blank", "Audio", "Communication", "IDE Based Coding"
                ]

                CODING_SECTION_TYPES = {
                    "coding", "ide based coding", "sql", "sql coding", "web coding"
                }

                all_valid = True
                for i, sec in enumerate(sections):
                    sec_num  = i + 1
                    sec_type = sec["section_type"]
                    matched  = next(
                        (v for v in valid_section_types
                         if sec_type.lower() == v.lower()), None)

                    cq = sec.get("coding_restriction",  "")
                    dl = sec.get("default_coding_lang", "")
                    is_coding = (matched or "").lower() in CODING_SECTION_TYPES

                    with st.expander(
                            f"📋 Section {sec_num}: {sec_type}", expanded=(i == 0)):

                        # Row 1 — core metadata
                        c1, c2, c3, c4 = st.columns(4)
                        with c1:
                            if matched:
                                st.success(f"**Type:** {matched}")
                                sec["section_type"] = matched
                                if matched.lower() in MARKS_SECTION_TYPES:
                                    st.info("🏅 Marks per Q: enabled")
                            else:
                                st.error(f"❌ Unknown type: '{sec_type}'")
                                all_valid = False
                        with c2:
                            st.info(f"**Name:** {sec['section_name'] or '(not set)'}")
                        with c3:
                            st.info(
                                f"**Time:** {sec['time_limit'] or '(not set)'} mins")
                        with c4:
                            q_count = len(sec["questions_df"])
                            st.info(f"**Questions:** {q_count} rows")

                        # Row 2 — coding language settings (always shown for coding sections)
                        if is_coding:
                            st.markdown("---")
                            lc1, lc2 = st.columns(2)
                            with lc1:
                                if cq:
                                    st.success(f"🖥️ **Select Coding Languages:** {cq}")
                                else:
                                    st.warning("🖥️ **Select Coding Languages:** *(not set in CSV)*")
                            with lc2:
                                if dl:
                                    st.success(f"🌐 **Default Coding Language:** {dl}")
                                else:
                                    st.warning("🌐 **Default Coding Language:** *(not set in CSV)*")

                        if not sec["questions_df"].empty:
                            st.dataframe(
                                sec["questions_df"].head(10), use_container_width=True)

                st.write("---")

                if not all_valid:
                    st.error(
                        "❌ One or more sections have invalid Section Types. "
                        f"Valid types: {', '.join(valid_section_types)}")
                else:
                    with st.spinner(
                            f"🤖 Running automation for {len(sections)} section(s)..."):
                        run_automation(mob, otp_val, sections, wait_time=wait_time)

        except Exception as e:
            import traceback
            err_str = str(e)
            tb_str  = traceback.format_exc()
            if "element not interactable" in err_str or "WebDriver" in err_str \
                    or "chromedriver" in err_str.lower() \
                    or "session" in err_str.lower():
                st.error(f"❌ Browser Error: {err_str[:400]}")
            else:
                st.error(f"❌ Error: {err_str[:400]}")
            with st.expander("🔍 Full traceback"):
                st.code(tb_str)