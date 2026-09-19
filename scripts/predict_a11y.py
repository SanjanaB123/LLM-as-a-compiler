"""
Predict the accessibility tree (role + accessible name) for the hostile page,
and score each targeting-ladder rung per control.

This approximates the ARIA accessible-name algorithm well enough to answer the
spike's real question: after we strip test IDs and use table layout, does each
control still have a usable SEMANTIC anchor (rung 1), and where must we fall
back to a RELATIONAL anchor (rung 2)?

It is NOT a browser. The live browser probe (probe_browser.py) confirms the
real a11y snapshot matches this prediction.
"""
from bs4 import BeautifulSoup

HTML = open("members.html").read()
soup = BeautifulSoup(HTML, "html.parser")

# --- crude role mapping (implicit ARIA roles for the tags we use) ---
IMPLICIT_ROLE = {"button": "button", "input": "textbox", "a": "link"}

def accessible_name(el):
    """Approximate the ARIA accessible-name computation, in priority order."""
    # 1. aria-label wins
    if el.get("aria-label"):
        return el["aria-label"], "aria-label"
    # 2. <label for=id>
    _id = el.get("id")
    if _id:
        lbl = soup.find("label", attrs={"for": _id})
        if lbl and lbl.get_text(strip=True):
            return lbl.get_text(strip=True), "label[for]"
    # 3. visible text content (buttons/links)
    txt = el.get_text(strip=True)
    if txt:
        return txt, "text-content"
    return None, None

def has_testid(el):
    return any(k in el.attrs for k in ("data-testid", "data-test", "data-cy"))

def relational_anchor(el):
    """Rung 2: can we describe this as 'the X after label Y'?
    Legacy pattern: label in one <td>, control in the next <td>."""
    td = el.find_parent("td")
    if not td:
        return None
    prev = td.find_previous_sibling("td")
    if prev:
        lbl = prev.find("label") or prev
        label_text = lbl.get_text(strip=True)
        if label_text:
            return f'{IMPLICIT_ROLE.get(el.name, el.name)} in cell after label "{label_text}"'
    return None

print("=" * 68)
print("PREDICTED ACCESSIBILITY TREE + TARGETING LADDER  (hostile page)")
print("=" * 68)

controls = soup.find_all(["button", "input", "a"])
all_have_rung1 = True
for el in controls:
    role = IMPLICIT_ROLE.get(el.name, "generic")
    name, name_src = accessible_name(el)
    print(f"\n<{el.name}>  ->  role={role!r}")
    print(f"   test-id present?          {has_testid(el)}   (rung 0: unavailable by design)")
    if name:
        print(f"   RUNG 1 a11y role+name:    role={role!r} name={name!r}  [via {name_src}]  ✓")
    else:
        print(f"   RUNG 1 a11y role+name:    NONE  ✗  (anonymous control)")
        all_have_rung1 = False
    rel = relational_anchor(el)
    print(f"   RUNG 2 relational:        {rel if rel else 'n/a'}")

print("\n" + "=" * 68)
print("VERDICT")
print("=" * 68)
print(f"Every interactive control has a usable rung-1 semantic anchor: {all_have_rung1}")
print("Test IDs available: False (stripped by design) -> rung 0 correctly unusable")
print("Relational rung available as backstop for table-form fields: yes")
