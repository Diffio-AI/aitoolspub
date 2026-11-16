"""JavaScript run inside pages (every frame). Reads forms without any model in the loop.

SURVEY stamps each field with `data-bb="<id>"` (kept across re-surveys, so ids are stable while the DOM
lives) and returns fields, buttons, errors and page context. The same call doubles as the readback: every
field's current value is part of what it returns.
"""

SURVEY = r"""
(opts) => {
  const prefix = opts.prefix || "";
  const maxText = opts.maxText ?? 3000;
  const maxOptions = opts.maxOptions ?? 60;
  const clean = s => (s || "").replace(/\s+/g, " ").trim();
  const cut = (s, n) => (s.length > n ? s.slice(0, n) + "…" : s);
  if (window.__bbNext === undefined) window.__bbNext = 1;
  if (window.__bbBtnNext === undefined) window.__bbBtnNext = 1;

  const shown = el => {
    if (!el || !el.isConnected) return false;
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      const s = getComputedStyle(e);
      if (s.display === "none" || s.visibility === "hidden" || s.visibility === "collapse") return false;
      // aria-hidden that a popover library put on the rest of the page while it is open (the aria-hidden package
      // Radix uses marks it data-aria-hidden) hides nothing on screen: the form is still there behind it.
      if (e.hidden || e.getAttribute("aria-hidden") === "true" && e !== el && !e.hasAttribute("data-aria-hidden")) return false;
    }
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  // Checkboxes, radios and file inputs are often visually hidden behind a styled label or button.
  const usable = el => {
    if (shown(el)) return true;
    const t = (el.type || "").toLowerCase();
    if (!["checkbox", "radio", "file"].includes(t)) return false;
    if (el.labels && [...el.labels].some(shown)) return true;
    return shown(el.parentElement);
  };
  const textOf = (node, extra) => {
    if (!node) return "";
    const c = node.cloneNode(true);
    c.querySelectorAll("input,select,textarea,button,option,script,style,[data-bb-skip]" + (extra ? "," + extra : "")).forEach(x => x.remove());
    // Words in separate elements with nothing between them ("First Name<span>required</span>", the "(required)"
    // drawn by CSS on Zapier's forms) get a space, so the label doesn't read "First Namerequired".
    let out = "", prevParent = null;
    const walk = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
    for (let t = walk.nextNode(); t; t = walk.nextNode()) {
      const v = t.nodeValue;
      if (!v) continue;
      if (out && t.parentNode !== prevParent && /[\p{L}\p{N}?!:.)]$/u.test(out) && /^[\p{L}\p{N}]/u.test(v)) out += " ";
      out += v;
      prevParent = t.parentNode;
    }
    return clean(out);
  };
  const LABEL_STRIP = ".help,.hint,.error,.description,small,[class*='help'],[class*='hint'],[class*='error']";
  const byIds = ids => clean((ids || "").split(/\s+/).map(id => textOf(document.getElementById(id))).join(" "));

  const FIELD_SEL = "input:not([type=hidden]), textarea, select, [contenteditable=''], [contenteditable='true']";
  // The field's own container: the highest ancestor (up to 4 levels) holding no other field.
  const ownWrap = el => {
    let w = null;
    for (let a = el.parentElement, i = 0; a && i < 4 && a.tagName !== "FORM" && a !== document.body; a = a.parentElement, i++) {
      const others = [...a.querySelectorAll(FIELD_SEL)].filter(x => x !== el && !(x.type === "radio" && x.name && x.name === el.name)
                                                                   && !(x.type === "checkbox" && x.name && x.name === el.name));
      if (others.length) break;
      w = a;
    }
    return w;
  };
  const labelOf = el => {
    let t = byIds(el.getAttribute("aria-labelledby"));
    // A form rendered twice with the same ids (Getting Smart's WPForms) gives the input both copies' labels.
    if (!t && el.labels && el.labels.length) t = clean([...new Set([...el.labels].map(l => clean(textOf(l, LABEL_STRIP) || textOf(l))))].join(" "));
    if (!t) t = clean(el.getAttribute("aria-label"));
    if (!t) {
      // Short text in the field's own wrapper (formless React forms often use a bare <div>).
      const w = ownWrap(el);
      const x = w ? textOf(w, LABEL_STRIP) : "";
      if (x && x.length <= 120) t = x;
    }
    if (!t) t = clean(el.getAttribute("placeholder") || el.getAttribute("title") || el.name || "");
    // "Choose image…" says what to click, not what the file is for: add the text just before it.
    if (/^(choose|upload|browse|select|add|drop|attach)\b/i.test(t) && el.labels && el.labels[0]) {
      const prev = el.labels[0].previousElementSibling;
      const p = prev && !prev.querySelector(FIELD_SEL) ? textOf(prev) : "";
      if (p && p.length <= 80) t = p + ": " + t;
    }
    return t;
  };
  // Elements right after the field (flat layouts: <label><input><span class=help>), up to the next field.
  const after = el => {
    const out = [];
    const n = el.closest("label") || el;  // a field wrapped in its label: look after the label
    for (let i = 0, x = n.nextElementSibling; x && i < 4; i++, x = x.nextElementSibling) {
      if (x.matches(FIELD_SEL + ", label, fieldset, button") || x.querySelector(FIELD_SEL)) break;
      out.push(x);
    }
    return out;
  };
  const near = (el, sel) => {
    const w = ownWrap(el);
    const found = w ? [...w.querySelectorAll(sel)] : [];
    for (const x of after(el)) { if (x.matches(sel)) found.push(x); found.push(...x.querySelectorAll(sel)); }
    return [...new Set(found)];
  };
  const HELP_SEL = "small, .help, .hint, .description, .form-text, [class*='help'], [class*='hint']";
  const ERR_SEL = "[role=alert], .error, .errors, .invalid-feedback, .field-error, [class*='error']";
  const helpOf = (el, label) => {
    const parts = [];
    const d = byIds(el.getAttribute("aria-describedby"));
    if (d) parts.push(d);
    const inLabels = el.labels ? [...el.labels].flatMap(l => [...l.querySelectorAll(HELP_SEL)]) : [];
    for (const h of [...inLabels, ...near(el, HELP_SEL)]) {
      const x = textOf(h);
      if (x && x !== label && !parts.some(p => p.includes(x)) && shown(h) && !h.matches(ERR_SEL)) parts.push(x);
    }
    return cut(clean(parts.join(" ")), 400);
  };
  const badRe = /\b(error|fail(ed|ure)?|invalid|required|denied|not |unable|could ?n[o']t|try again|problem|wrong|fehler|erreur)/i;
  // Kaggle's MUI fields wrap their standing hint ("Minimum of 7 characters") in role=alert, so every hint read as an
  // error. A message inside a field's aria-describedby hint is an error only once the field is aria-invalid, the
  // hint carries an error class, or it reads like one.
  const quietHints = [...document.querySelectorAll("[aria-describedby]")].filter(f => f.getAttribute("aria-invalid") !== "true")
    .flatMap(f => f.getAttribute("aria-describedby").split(/\s+/).map(id => id && document.getElementById(id)).filter(Boolean));
  // Fillout gives every field a role=alert box that reads just "Your Name:" until there is a message: a label
  // ending in a colon, with nothing after it, is the message's prefix, not an error.
  const bareLabel = x => x.length < 80 && /:\s*$/.test(x) && !badRe.test(x);
  const isHint = h => quietHints.some(d => {
    if (!d.contains(h) || badRe.test(textOf(h))) return false;
    for (let n = h; n && n !== d.parentElement; n = n.parentElement)
      if (/error|invalid|danger/i.test(typeof n.className === "string" ? n.className : "")) return false;
    return true;
  });
  const errorOf = el => {
    const parts = [];
    if (el.getAttribute("aria-invalid") === "true") {
      const d = byIds(el.getAttribute("aria-errormessage") || "");
      if (d) parts.push(d);
    }
    for (const h of near(el, ERR_SEL)) {
      // The field itself or another control is never the message, whatever its classes say (Tailwind's
      // "data-[errored=true]:border-destructive" on Zapier's select button matched [class*='error']).
      if (h === el || h.contains(el) || h.matches("button, input, select, textarea, [role=combobox], [role=button]") || isHint(h)) continue;
      const x = textOf(h);
      if (x && shown(h) && !bareLabel(x) && !parts.includes(x)) parts.push(x);
    }
    return cut(clean(parts.join(" ")), 300);
  };

  const stamp = (el, cls) => {
    if (!el.dataset.bb) el.dataset.bb = prefix + (cls === "b" ? "b" + window.__bbBtnNext++ : "f" + window.__bbNext++);
    return el.dataset.bb;
  };
  const isRich = el => el.isContentEditable && !(el.parentElement && el.parentElement.isContentEditable);
  const editorKind = el => {
    const c = (el.className || "") + " " + (el.closest("[class]") ? el.closest("[class]").className : "");
    if (/ProseMirror/.test(c)) return "prosemirror";
    if (/ql-editor/.test(c)) return "quill";
    if (el.tagName === "TRIX-EDITOR") return "trix";
    if (el.closest("[data-lexical-editor]") || el.getAttribute("data-lexical-editor")) return "lexical";
    return "contenteditable";
  };
  const TAGGY = /(^|[\s_-])(tags?|chips?|tokens?|pills?|keywords?)([\s_-]|$)/i;
  const chipSel = CHIP_SEL;
  const tagWrap = el => {
    // Only ancestors that hold no other field: a chip elsewhere in the form says nothing about this input.
    const top = ownWrap(el);
    if (!top) return null;
    for (let w = el.parentElement; w; w = w.parentElement) {
      if (w.querySelector(chipSel) || TAGGY.test(w.className || "") || TAGGY.test(w.id || "")) return w;
      if (w === top) break;
    }
    return null;
  };
  // A multi-select combobox (react-select isMulti) shows each pick as a chip beside its input, which clears.
  const multiSel = "[class*='multi-value'], [class*='multiValue'], [class*='MultiValue']";
  const multiChips = el => {
    const top = ownWrap(el);
    for (let w = el.parentElement; w; w = w.parentElement) {
      const cs = [...w.querySelectorAll(multiSel)].filter(c => !(c.parentElement && c.parentElement.closest(multiSel)
        && w.contains(c.parentElement.closest(multiSel))));
      if (cs.length) return cs.filter(shown).map(c => textOf(c).replace(/\s*[×✕]\s*$/, "").trim()).filter(Boolean);
      if (w === top || !top) break;
    }
    return null;
  };
  const chipsIn = w => chipTexts(w, c => shown(c) && !c.contains(document.activeElement && document.activeElement.tagName === "INPUT" ? document.activeElement : null));

  const TEXTISH = new Set(["", "text", "email", "url", "tel", "number", "search", "date", "datetime-local", "month",
                           "time", "week", "password", "color", "range"]);
  const candidates = [...document.querySelectorAll(
    "input, textarea, select, [contenteditable=''], [contenteditable='true'], [role=textbox], [role=combobox]")];
  const groups = new Map();  // form element (or null) -> {fields, radios: Map(name -> field)}
  const groupOf = el => {
    const f = el.form || el.closest("form");
    if (!groups.has(f)) groups.set(f, { form: f, fields: [], named: new Map() });
    return groups.get(f);
  };

  // The clickable zone of a Dropzone uploader that this read-only or hidden input belongs to, if any.
  const dropzoneOf = el => {
    if (!(el.readOnly || el.disabled || /dropzone/i.test(el.className))) return null;
    for (let a = el.parentElement, i = 0; a && i < 3; a = a.parentElement, i++) {
      const zs = a.querySelectorAll(".dz-clickable, .dropzone");
      if (zs.length === 1) return zs[0];
      if (zs.length > 1) return null;
    }
    return null;
  };

  for (const el of candidates) {
    const tag = el.tagName;
    let type = tag === "INPUT" ? (el.getAttribute("type") || "text").toLowerCase() : "";
    if (tag === "INPUT" && ["hidden", "submit", "button", "image", "reset"].includes(type)) continue;
    if (el.closest("[data-bb-skip]")) continue;
    if (el.classList.contains("dz-hidden-input")) continue;  // Dropzone's own chooser input; its zone is the field
    // A custom select's value carrier (Base UI, Radix): hidden from assistive tech and the tab order; its visible
    // trigger is the field. Connectively's "Rows per page" carrier read as a tag input and blocked a press.
    if (el.getAttribute("aria-hidden") === "true" && el.tabIndex === -1 && !["checkbox", "radio", "file"].includes(type)) continue;
    if (tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") {
      if (el.isContentEditable && !isRich(el)) continue;
      if (el.querySelector("input, textarea")) continue;  // role=combobox wrapper around a real input
      if (!el.isContentEditable && el.getAttribute("role") === "textbox") continue;
    }
    if (!usable(el)) continue;
    const g = groupOf(el);

    if (type === "radio" || (type === "checkbox" && el.name &&
        (el.form || document).querySelectorAll(`input[type=checkbox][name="${CSS.escape(el.name)}"]`).length > 1)) {
      const key = type + ":" + el.name;
      let f = g.named.get(key);
      if (!f) {
        const fs = el.closest("fieldset");
        const legend = fs && fs.querySelector("legend");
        const groupLabel = clean(legend ? textOf(legend) : byIds((el.closest("[role=radiogroup],[role=group]") || el).getAttribute("aria-labelledby")));
        f = { id: stamp(el, "f"), kind: type === "radio" ? "radio" : "checkboxes", name: el.name, label: groupLabel,
              required: false, options: [], _els: [] };
        g.named.set(key, f);
        g.fields.push(f);
      } else if (!el.dataset.bb) {
        el.dataset.bb = f.id;
      } else if (el.dataset.bb !== f.id) {
        el.dataset.bb = f.id;
      }
      f._els.push(el);
      el.dataset.bbOpt = String(f.options.length);
      const optLabel = labelOf(el);
      f.options.push({ value: el.value, label: optLabel, selected: el.checked });
      if (!f.label) f.label = "";
      f.required = f.required || el.required || el.getAttribute("aria-required") === "true";
      continue;
    }

    const id = stamp(el, "f");
    const label = labelOf(el);
    const f = { id, label, name: el.name || el.id || "", required: el.required || el.getAttribute("aria-required") === "true" };
    if (/\*\s*$/.test(label) || /\(required\)/i.test(label)) f.required = true;
    if (tag === "SELECT") {
      f.kind = el.multiple ? "multiselect" : "select";
      const opts_ = [...el.options].filter(o => !o.disabled || o.selected);
      f.options = opts_.slice(0, maxOptions).map(o => ({ value: o.value, label: clean(o.label || o.textContent), selected: o.selected }));
      if (opts_.length > maxOptions) f.more_options = opts_.length - maxOptions;
      // A selected option with an empty value is a placeholder ("Choose a category"): nothing chosen yet.
      const sel = [...el.selectedOptions].filter(o => o.value !== "").map(o => clean(o.label || o.textContent));
      f.value = el.multiple ? sel : (sel[0] ?? "");
    } else if (tag === "TEXTAREA") {
      f.kind = "textarea"; f.value = el.value;
    } else if (tag === "INPUT" && type === "checkbox") {
      f.kind = "checkbox"; f.value = el.checked;
    } else if (tag === "INPUT" && dropzoneOf(el)) {
      // A Dropzone uploader (WPForms and others): the input only holds the result; clicking the zone opens the
      // file chooser.
      const dz = dropzoneOf(el);
      dz.dataset.bbDz = id;
      f.kind = "file"; f.uploader = "dropzone";
      f.value = [...dz.querySelectorAll(".dz-filename")].map(x => clean(x.innerText)).filter(Boolean);
      if (dz.dataset.extensions) f.accept = dz.dataset.extensions.split(",").map(x => "." + x.trim()).join(",");
      if (+(dz.dataset.maxFileNumber || 1) > 1) f.multiple = true;
    } else if (tag === "INPUT" && type === "file") {
      f.kind = "file"; f.value = [...(el.files || [])].map(x => x.name);
      if (el.accept) f.accept = el.accept;
      if (el.multiple) f.multiple = true;
    } else if (el.isContentEditable) {
      f.kind = "richtext"; f.editor = editorKind(el); f.value = el.innerText.replace(/\n$/, "");
      if (!f.label) f.label = clean(el.getAttribute("data-placeholder") || el.getAttribute("aria-placeholder") || "");
    } else {
      f.kind = "text"; f.value = el.value;
      // A div or button combobox (F6S's year, Zapier Interfaces' Radix select) holds its pick as text, not a value
      // (a button's value is "").
      if ((f.value === undefined || tag === "BUTTON") && el.getAttribute("role") === "combobox") { f.value = clean(el.innerText || ""); f.shown = true; }
      if (type && type !== "text") f.input_type = type;
      const combo = el.getAttribute("role") === "combobox" || el.getAttribute("aria-autocomplete") || el.getAttribute("list");
      // A search box is never a tag input, whatever pill-styled buttons sit beside it in a toolbar.
      const search = type === "search" || el.getAttribute("role") === "searchbox" || !!el.closest("[role=search]")
        || /^search\b/i.test(clean(el.placeholder || "") + " " + label);
      const w = search ? null : tagWrap(el);
      if (w || (!search && TAGGY.test((el.name || "") + " " + (el.id || "") + " " + label + " " + (el.className || "")))) {
        f.kind = "tags"; f.typed = el.value;
        if (w) { w.dataset.bbWrap = id; f.value = chipsIn(w); }
      } else if (combo) {
        f.kind = "combobox";
        const picks = multiChips(el);
        if (picks) { f.multi = true; f.value = picks; f.shown = true; }
        else if (!el.value) {
          // react-select shows a single pick in a sibling div and leaves its input empty (Fillout's Submission Type
          // read back "" after the pick).
          const top = ownWrap(el);
          for (let w = el.parentElement; w; w = w.parentElement) {
            const sv = w.querySelector("[class*='single-value'], [class*='singleValue'], [class*='SingleValue']");
            if (sv && shown(sv)) { f.value = clean(textOf(sv)); f.shown = true; break; }
            if (w === top || !top) break;
          }
        }
      }
      if (el.getAttribute("list")) {
        const dl = document.getElementById(el.getAttribute("list"));
        if (dl) f.options = [...dl.options].slice(0, maxOptions).map(o => ({ value: o.value, label: clean(o.label || o.value) }));
      }
    }
    for (const a of ["maxlength", "minlength", "pattern", "min", "max", "placeholder"]) {
      const v = el.getAttribute(a);
      if (v !== null && v !== "") f[a] = /length$/.test(a) ? Number(v) : v;
    }
    if ((el.disabled || el.readOnly) && !f.uploader) f.disabled = true;
    const help = helpOf(el, label);
    if (help) f.help = help;
    const err = errorOf(el);
    if (err) f.error = err;
    if (opts.validity && el.willValidate && !el.checkValidity() && el.validationMessage) f.invalid = el.validationMessage;
    // Chrome only enforces minlength/maxlength on edits it thinks a user made, so check them here.
    if (opts.validity && !f.invalid && typeof el.value === "string" && el.value.length) {
      if (el.minLength > 0 && el.value.length < el.minLength) f.invalid = `Use at least ${el.minLength} characters (now ${el.value.length}).`;
      if (el.maxLength > 0 && el.value.length > el.maxLength) f.invalid = `Use at most ${el.maxLength} characters (now ${el.value.length}).`;
    }
    if (f.kind === "text" && f.input_type === "password") f.secret = true;
    if (el.dataset.cgId) f.captcha = true;  // captcha-gate found an image captcha asking for this field
    f._els = [el];
    g.fields.push(f);
  }

  // Group-level required/errors for radio and checkbox groups.
  for (const g of groups.values()) for (const f of g.fields) {
    if (f.kind === "radio" || f.kind === "checkboxes") {
      const errs = f._els.map(errorOf).filter(Boolean);
      if (errs.length) f.error = [...new Set(errs)].join(" ");
      const inv = opts.validity && f._els.find(e => e.willValidate && !e.checkValidity());
      if (inv && inv.validationMessage) f.invalid = inv.validationMessage;
      if (f.kind === "radio") f.value = (f.options.find(o => o.selected) || {}).label || "";
      else f.value = f.options.filter(o => o.selected).map(o => o.label);
      if (!f.label) {
        const wrap = f._els[0].closest("fieldset, [role=radiogroup], [role=group], .field, .form-group") || f._els[0].parentElement.parentElement;
        f.label = wrap ? cut(textOf(wrap.querySelector("legend, label, .label, p, span") || wrap), 120) : f.name;
      }
    }
  }

  const btnLabel = b => clean(b.innerText || b.value || b.getAttribute("aria-label") || b.title || "");
  const lca = els => {
    if (!els.length) return null;
    let a = els[0];
    while (a && !els.every(e => a.contains(e))) a = a.parentElement;
    return a;
  };
  // A form with nothing to fill but a button still matters: a confirm or claim step, or a check whose only input is
  // a captcha widget.
  for (const fm of document.forms) {
    if (!groups.has(fm) && shown(fm) && !fm.closest("[data-bb-skip]")) groups.set(fm, { form: fm, fields: [], named: new Map() });
  }
  const out = [];
  let gi = 0;
  const allEls = [...groups.values()].flatMap(x => x.fields.flatMap(f => f._els));
  // Script-only controls: a span or div styled as clickable (Manta's "+ Add Service Area" span, which the form
  // requires), with short text of its own and no clickable ancestor. Inside it an icon and a label at most, and no
  // real control: Clubic's "Envoyer" is a pointer div around a pointer <a> without href, and Softonic's sidebar items
  // (11-02: "4.6.0", the only way to a rejected version) are a pointer div holding an icon div and a <p>.
  const scriptControls = scope => {
    const res = [];
    for (const e of scope.querySelectorAll("span, div, li, a:not([href])")) {
      const raw = e.textContent || "";
      if (!raw.trim() || raw.length > 200) continue;
      const t = clean(raw);
      if (!t || t.length > 40 || e.querySelectorAll("*").length > 8) continue;
      if (e.querySelector("button, input, select, textarea, a[href], [role=button]")) continue;
      if (e.closest("button, [role=button], a[href], label, [role=option], [role=listbox], [role=combobox], [contenteditable]")) continue;
      if (getComputedStyle(e).cursor !== "pointer") continue;
      if (e.parentElement && getComputedStyle(e.parentElement).cursor === "pointer") continue;
      res.push(e);
    }
    return res;
  };
  const buttonsIn = (g, scope) => {
    const buttons = [];
    const seen = new Set();
    const bs = [...scope.querySelectorAll("button, input[type=submit], input[type=button], input[type=image], [role=button], a.button, a.btn")];
    if (g.form && g.form.id) bs.push(...document.querySelectorAll(`[form="${CSS.escape(g.form.id)}"]`));
    bs.push(...scriptControls(scope));
    for (const b of bs) {
      if (seen.has(b) || !shown(b) || b.closest("[data-bb-skip]")) continue;
      seen.add(b);
      if (g.fields.some(f => f._els.some(e => e.closest && b.contains(e)))) continue;
      const text = btnLabel(b);
      if (!text) continue;
      const type = b.tagName === "BUTTON" ? (b.getAttribute("type") || (g.form ? "submit" : "button")) : (b.type || "button");
      buttons.push({ id: stamp(b, "b"), text: cut(text, 80), type, ...(b.disabled ? { disabled: true } : {}) });
    }
    return buttons;
  };
  for (const g of groups.values()) {
    if (!g.fields.length && !g.form) continue;
    const scope = g.form || lca(g.fields.flatMap(f => f._els));
    let buttons = scope ? buttonsIn(g, scope) : [];
    // The button can sit outside the fields' box (a modal footer next to its body, a submit after the form):
    // widen a few levels until one turns up, stopping before taking in another group's fields.
    const own = new Set(g.fields.flatMap(f => f._els));
    for (let s = scope, i = 0; scope && !buttons.length && i < 4 && s.parentElement && s !== document.body; i++) {
      s = s.parentElement;
      if (allEls.some(e => !own.has(e) && s.contains(e))) break;
      if ([...s.querySelectorAll("form")].some(f => f !== g.form && groups.has(f))) break;
      buttons = buttonsIn(g, s);
    }
    if (!g.fields.length && !buttons.length) continue;
    const fields = g.fields.map(f => { const { _els, ...rest } = f; return rest; });
    out.push({ group: prefix + "g" + (gi++), form: g.form ? { action: g.form.getAttribute("action") || "", method: (g.form.method || "get").toLowerCase(),
                                                          id: g.form.id || "" } : null, fields, buttons });
  }

  // What covers the page takes every click: a modal (Softonic's "What's new" dialog over its Add App button) or a
  // fixed full-screen layer, which may be an iframe (heise's consent wall, whose buttons live in that frame). Its
  // controls are listed as the overlay; buttons outside any form are listed as page_buttons.
  const grouped = new Set(out.flatMap(g => g.buttons.map(b => b.id)));
  const ownFields = [...groups.values()].flatMap(x => x.fields.flatMap(f => f._els));
  const iconLabel = b => {
    const c = `${typeof b.className === "string" ? b.className : ""} ${b.id} ${b.getAttribute("data-testid") || ""}`;
    return /close|dismiss/i.test(c) ? "close (icon)" : "";
  };
  const controls = (scope, limit, skip) => {
    const res = [];
    // Real buttons first; script-only controls (a sidebar item, an app card) fill what the limit leaves.
    const real = [...scope.querySelectorAll("button, input[type=submit], input[type=button], [role=button], a.button, a.btn, [class*=close], [class*=dismiss]")];
    for (const b of [...real, ...scriptControls(scope).filter(e => !real.some(r => r.contains(e) || e.contains(r)))]) {
      if (res.length >= limit) break;
      if ((skip && skip.contains(b)) || !shown(b) || b.closest("[data-bb-skip]")) continue;
      if (b.parentElement && b.parentElement.closest("button, [role=button]")) continue;
      const text = btnLabel(b) || iconLabel(b);
      if (!text) continue;
      const id = stamp(b, "b");
      if (grouped.has(id) || res.some(x => x.id === id)) continue;
      res.push({ id, text: cut(text, 60) });
    }
    return res;
  };
  const cover = (() => {
    if (opts.asOverlay) return document.body;
    const area = innerWidth * innerHeight;
    for (const [x, y] of [[0.5, 0.5], [0.5, 0.3], [0.5, 0.7]]) {
      const hit = document.elementFromPoint(innerWidth * x, innerHeight * y);
      if (hit && hit.closest("[data-state=closed]")) continue;  // a popover on its way out (Radix keeps it while it animates)
      for (let e = hit; e && e !== document.body && e !== document.documentElement; e = e.parentElement) {
        // A layer whose content is all closing popovers is on its way out.
        if (e.children.length && [...e.children].every(c => c.matches("[data-state=closed]"))) break;
        const modal = e.matches("dialog[open], [role=dialog], [role=alertdialog], [aria-modal=true]");
        if (!modal && getComputedStyle(e).position !== "fixed") continue;
        const r = e.getBoundingClientRect();
        if (!modal && (r.width * r.height < area * 0.2 || ownFields.some(f => e.contains(f)))) continue;  // a header, an app shell
        return hit.tagName === "IFRAME" ? hit : e;   // a layer holding a frame (Sourcepoint's container div)
      }
    }
    return null;
  })();
  let overlay = null;
  if (cover && cover.tagName === "IFRAME") {
    cover.dataset.bbCover = "1";
    overlay = { frame: cover.src || "" };
  } else if (cover) {
    const text = cut(clean(cover.innerText || ""), 300), buttons = controls(cover, 12);
    // reCAPTCHA's image challenge lays a see-through full-screen layer over the page (PLSN's submissions form), with
    // nothing in it to press: the captcha is solved before a press, so the page's own buttons are still the ones.
    const challenge = !text && !buttons.length && [...document.querySelectorAll("iframe")].some(f =>
      shown(f) && /recaptcha\/(api2|enterprise)\/bframe|hcaptcha\.com\/.*challenge|challenges\.cloudflare\.com/.test(f.src || ""));
    overlay = challenge ? { captcha: true } : text || buttons.length ? { text, buttons } : { empty: true };
  }
  const pageButtons = prefix ? [] : controls(document, 20, cover);

  // Toast libraries give every toast role=alert, a success too (Softonic's "App created ... successfully" came
  // back as an error). Those go under notices.
  const pageErrors = [], notices = [];
  const okRe = /\b(success(fully)?|saved|created|submitted|sent|updated|added|thank(s| you)|merci|danke|erfolgreich|gespeichert|envoy[ée])\b/i;
  document.querySelectorAll("[role=alert], [aria-live=assertive], .alert-danger, .alert-error, .error-summary, .flash-error, .errors").forEach(e => {
    const x = textOf(e);
    if (!x || !shown(e) || x.length >= 500) return;
    // A live region is where messages appear, not one itself: WPForms marks its Submit button aria-live (Getting
    // Smart's page listed "Submit" as an error).
    if (e.matches("button, input, select, textarea, a, label") || isHint(e) || bareLabel(x)) return;
    const cls = [e, e.parentElement, e.parentElement && e.parentElement.parentElement]
      .map(n => n && typeof n.className === "string" ? n.className : "").join(" ");
    const good = (/success|--info\b|toast-info/i.test(cls) && !/error|danger|warn/i.test(cls))
      || (okRe.test(x) && !badRe.test(x) && !/error|danger/i.test(cls));
    const into = good ? notices : pageErrors;
    if (!into.includes(x)) into.push(x);
  });
  const step = (() => {
    const cur = document.querySelector("[aria-current=step]");
    if (cur) return clean(cur.innerText);
    const m = (document.body ? document.body.innerText : "").match(/step\s+\d+\s*(of|\/)\s*\d+/i);
    return m ? m[0] : "";
  })();
  const headings = [...document.querySelectorAll("h1, h2, h3")].filter(shown).map(h => cut(clean(h.innerText), 120)).filter(Boolean).slice(0, 15);
  const body = document.body ? clean(document.body.innerText) : "";
  return {
    url: location.href, title: document.title, headings, step, errors: pageErrors, groups: out,
    ...(notices.length ? { notices } : {}),
    ...(overlay ? { overlay } : {}), ...(pageButtons.length ? { page_buttons: pageButtons } : {}),
    text: maxText ? cut(body, maxText) : "", text_length: body.length,
  };
}
"""

# Values that typed-in text cannot be compared without: what's in each stamped tag wrapper right now.
TAG_CHIPS = r"""
(id) => {
  const w = document.querySelector(`[data-bb-wrap="${CSS.escape(id)}"]`);
  if (!w) return null;
  const chips = chipTexts(w, c => c.getBoundingClientRect().width > 0);
  const hidden = [...w.querySelectorAll("input[type=hidden]")].map(i => i.value).filter(Boolean);
  return { chips, hidden };
}
"""

READ = r"""
(opts) => {
  const clean = s => (s || "").replace(/[ \t]+/g, " ").replace(/\n\s*\n+/g, "\n\n").trim();
  const root = (opts.selector && document.querySelector(opts.selector)) || document.querySelector("main, [role=main], article") || document.body;
  const text = clean(root ? root.innerText : "");
  const links = [];
  if (opts.links) {
    const seen = new Set();
    const want = (opts.hrefContains || "").toLowerCase();
    const wantText = (opts.linkText || "").toLowerCase().replace(/\s+/g, " ").trim();
    for (const a of document.querySelectorAll("a[href]")) {
      const t = (a.innerText || a.getAttribute("aria-label") || a.title || "").replace(/\s+/g, " ").trim();
      const r = a.getBoundingClientRect();
      if (wantText && !t.toLowerCase().includes(wantText)) continue;
      if (want) {
        // Link checks look at every matching link, shown or not, with what search engines read from it.
        if (!a.href.toLowerCase().includes(want)) continue;
      } else if (!t || r.width === 0 || seen.has(a.href)) continue;
      seen.add(a.href);
      const l = { text: t.slice(0, 100), href: a.href };
      if (a.rel) l.rel = a.rel;
      if (want) { l.visible = r.width > 0; if (a.target) l.target = a.target; }
      links.push(l);
      if (links.length >= opts.links) break;
    }
  }
  return { url: location.href, title: document.title, text: text.slice(opts.offset || 0, (opts.offset || 0) + opts.maxText),
           text_length: text.length, links };
}
"""

# Cloudflare's email obfuscation shows "[email protected]" and keeps the address XOR-encoded in data-cfemail (and in
# /cdn-cgi/l/email-protection#<hex> links). Decode them in place first, so surveys and reads show the real
# address the site publishes.
CF_EMAILS = r"""
  {
    const dec = h => { try { const k = parseInt(h.slice(0, 2), 16); let s = "";
      for (let i = 2; i < h.length; i += 2) s += String.fromCharCode(parseInt(h.slice(i, i + 2), 16) ^ k);
      return /^[^@\s]+@[^@\s]+$/.test(s) ? s : null; } catch (e) { return null; } };
    for (const el of document.querySelectorAll("[data-cfemail]")) {
      const s = dec(el.getAttribute("data-cfemail")); if (s) { el.textContent = s; el.removeAttribute("data-cfemail"); }
    }
    for (const a of document.querySelectorAll("a[href*='/cdn-cgi/l/email-protection#']")) {
      const s = dec(a.getAttribute("href").split("#")[1] || ""); if (s) a.setAttribute("href", "mailto:" + s);
    }
  }
"""
# The chips in a tag input, as the survey shows them and as fill reads them back.
CHIPS = r"""
  const CHIP_SEL = "[class*='chip'], [class*='tag-'], [class~='tag'], [class*='token'], [class*='pill'], [data-tag]";
  const chipClean = s => (s || "").replace(/\s+/g, " ").trim();
  // A chip's remove control isn't its text, and a chip-classed list whose items have no chip class is several chips
  // (Academia.edu's research interests read back as one "Speech enhancement ×RemoveAudio Signal Processing ×Remove...").
  const isRemover = n => n.matches("button, [role=button], input")
    || /remove|delete/i.test(n.getAttribute("aria-label") || n.getAttribute("title") || "")
    || /^(×|✕|✖|remove|delete)$/i.test(chipClean(n.textContent));
  const removers = n => [...n.querySelectorAll("*")].filter(isRemover).filter((x, _, a) => !a.some(y => y !== x && y.contains(x)));
  const chipText = c => {
    const k = c.cloneNode(true);
    [...k.querySelectorAll("*")].forEach(x => { if (k.contains(x) && isRemover(x)) x.remove(); });
    return chipClean(k.textContent).replace(/\s*[×✕✖]\s*$/, "");
  };
  const splitChip = c => {
    let n = c;
    while (n.children.length === 1 && !isRemover(n.children[0])) n = n.children[0];
    const kids = [...n.children].filter(k => removers(k).length === 1);
    return kids.length >= 2 ? kids : [c];
  };
  const chipTexts = (w, keep) => [...w.querySelectorAll(CHIP_SEL)].filter(c => !c.querySelector(CHIP_SEL) && keep(c))
    .flatMap(splitChip).map(chipText).filter(Boolean);
"""
SURVEY = SURVEY.replace("(opts) => {", "(opts) => {" + CF_EMAILS + CHIPS, 1)
TAG_CHIPS = TAG_CHIPS.replace("(id) => {", "(id) => {" + CHIPS, 1)
READ = READ.replace("(opts) => {", "(opts) => {" + CF_EMAILS, 1)
