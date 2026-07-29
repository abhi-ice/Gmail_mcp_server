#!/usr/bin/env python3
"""
Convert .eml files (as produced by the gmail_export_email MCP tool) into PDFs.

Deliberately client-side: rendering HTML needs a layout engine, and putting one
in the Railway image would bloat the container for something the client can do.

    python scripts/eml_to_pdf.py message.eml
    python scripts/eml_to_pdf.py message.eml out.pdf
    python scripts/eml_to_pdf.py ./folder_of_emls/          # batch
    python scripts/eml_to_pdf.py msg.eml --save-attachments

Requires PyMuPDF (pip install pymupdf).
"""
from __future__ import annotations

import email
import email.policy
import html as html_mod
import os
import re
import sys
import tempfile
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("PyMuPDF is required:  pip install pymupdf")

EXT_BY_MIME = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/bmp": ".bmp", "image/webp": ".webp",
}


def dec(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def esc(s: str) -> str:
    return html_mod.escape(s or "")


def pick_bodies(msg) -> tuple[str, str, list, list]:
    """Return (html_body, text_body, inline_parts, attachment_parts)."""
    html_body, text_body, inline, attach = "", "", [], []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        ctype = (part.get_content_type() or "").lower()
        disp = (part.get_content_disposition() or "").lower()
        cid = (part.get("Content-ID") or "").strip("<>")
        filename = part.get_filename()

        if disp == "attachment" or (filename and not cid):
            attach.append(part)
            continue
        if cid or (disp == "inline" and ctype.startswith("image/")):
            inline.append(part)
            continue
        if ctype == "text/html" and not html_body:
            html_body = part.get_content()
        elif ctype == "text/plain" and not text_body:
            text_body = part.get_content()
    return html_body, text_body, inline, attach


def stage_inline_images(inline_parts, workdir: Path) -> dict:
    """Write inline images to disk; return {cid: filename} for src rewriting."""
    mapping = {}
    for i, part in enumerate(inline_parts):
        cid = (part.get("Content-ID") or "").strip("<>")
        ctype = (part.get_content_type() or "").lower()
        ext = EXT_BY_MIME.get(ctype, os.path.splitext(part.get_filename() or "")[1] or ".img")
        name = f"inline_{i}{ext}"
        try:
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            (workdir / name).write_bytes(payload)
        except Exception:
            continue
        if cid:
            mapping[cid] = name
        if part.get_filename():
            mapping[part.get_filename()] = name
    return mapping


def clean_html(body: str, cid_map: dict) -> str:
    """Strip active content and repoint cid: images at the staged files."""
    body = re.sub(r"(?is)<script.*?</script>", "", body)
    body = re.sub(r"(?is)<style.*?</style>", "", body)
    body = re.sub(r"(?is)<head.*?</head>", "", body)
    body = re.sub(r"(?is)</?(html|body|meta|link)[^>]*>", "", body)

    def fix_img(m):
        tag = m.group(0)
        sm = re.search(r'src=(["\'])(.*?)\1', tag, re.I)
        if not sm:
            return ""
        ref = sm.group(2)
        key = ref[4:] if ref.lower().startswith("cid:") else ref
        if key in cid_map:
            return re.sub(r'src=(["\']).*?\1', f'src="{cid_map[key]}"',
                          tag, count=1, flags=re.I)
        # remote tracking pixels / unresolvable cids: drop the tag so no
        # network fetch happens and no "[image]" placeholder is left behind
        return ""

    return re.sub(r"<img\b[^>]*>", fix_img, body, flags=re.I)


def auth_summary(msg) -> list[tuple[str, str]]:
    """Pull SPF/DKIM/DMARC verdicts out of Authentication-Results."""
    blob = " ".join(str(v) for v in (msg.get_all("Authentication-Results") or []))
    blob += " " + " ".join(str(v) for v in (msg.get_all("ARC-Authentication-Results") or []))
    out = []
    for mech in ("spf", "dkim", "dmarc"):
        m = re.search(rf"\b{mech}=(\w+)", blob, re.I)
        if m:
            out.append((mech.upper(), m.group(1).lower()))
    if msg.get("DKIM-Signature"):
        d = re.search(r"d=([^;\s]+)", str(msg.get("DKIM-Signature")))
        if d:
            out.append(("DKIM domain", d.group(1)))
    return out


def _chunk(s: str, n: int = 96) -> list[str]:
    """Hard-split a long unbreakable token so the layout engine can place it.

    DKIM/ARC signatures are ~400 chars of unbroken base64 with no space to wrap
    on; without this the story engine can never fit a line and loops forever.
    """
    return [s[i:i + n] for i in range(0, len(s), n)] or [""]


def headers_html(msg) -> str:
    """Verbatim header block — the part that actually evidences authenticity."""
    blocks = []
    for k, v in msg.items():
        val = re.sub(r"\s+", " ", str(v)).strip()
        parts = _chunk(val)
        blocks.append(
            f'<div style="font-family:monospace;font-size:7pt;margin:0 0 1px 0">'
            f'<b>{esc(k)}:</b> {esc(parts[0])}</div>'
        )
        for extra in parts[1:]:
            blocks.append(
                f'<div style="font-family:monospace;font-size:7pt;'
                f'margin:0 0 1px 14px;color:#333">{esc(extra)}</div>'
            )

    auth = auth_summary(msg)
    banner = ""
    if auth:
        cells = " &nbsp;&nbsp; ".join(f"<b>{esc(k)}</b>: {esc(v)}" for k, v in auth)
        banner = (
            f'<p style="font-family:sans-serif;font-size:8.5pt;color:#222;'
            f'margin:0 0 8px 0">Authentication results &mdash; {cells}</p>'
        )

    return (
        '<h3 style="font-family:sans-serif;font-size:11pt;margin:18px 0 4px 0">'
        'Original message headers (as received)</h3>'
        '<p style="font-family:sans-serif;font-size:8pt;color:#666;margin:0 0 8px 0">'
        'Reproduced verbatim from the exported .eml. The DKIM / SPF / ARC entries below '
        'are what establish that this message is authentic and unmodified.</p>'
        + banner
        + "".join(blocks)
    )


def build_html(msg, workdir: Path) -> tuple[str, list]:
    html_body, text_body, inline_parts, attach_parts = pick_bodies(msg)
    cid_map = stage_inline_images(inline_parts, workdir)

    subject = dec(msg.get("Subject", "")) or "(no subject)"
    date_raw = msg.get("Date", "")
    try:
        date_disp = parsedate_to_datetime(date_raw).strftime("%A, %d %B %Y  %H:%M %Z").strip()
    except Exception:
        date_disp = date_raw

    rows = [("From", dec(msg.get("From", ""))), ("To", dec(msg.get("To", "")))]
    if msg.get("Cc"):
        rows.append(("Cc", dec(msg.get("Cc"))))
    rows.append(("Date", date_disp))

    head = [
        f'<h2 style="margin:0 0 10px 0;font-family:sans-serif;font-size:15pt">{esc(subject)}</h2>',
        '<table style="font-family:sans-serif;font-size:9.5pt;margin-bottom:6px">',
    ]
    for k, v in rows:
        head.append(
            f'<tr><td style="color:#666;padding-right:10px;vertical-align:top"><b>{k}</b></td>'
            f'<td>{esc(v)}</td></tr>'
        )
    head.append("</table>")

    if attach_parts:
        names = ", ".join(esc(dec(p.get_filename() or "attachment")) for p in attach_parts)
        head.append(
            f'<p style="font-family:sans-serif;font-size:9pt;color:#444;margin:2px 0 8px 0">'
            f'<b>Attachments ({len(attach_parts)}):</b> {names}</p>'
        )
    head.append('<hr style="border:none;border-top:1px solid #bbb;margin:8px 0 12px 0"/>')

    if html_body:
        body = clean_html(html_body, cid_map)
    else:
        body = (f'<div style="font-family:sans-serif;font-size:10pt;white-space:pre-wrap">'
                f'{esc(text_body)}</div>')

    return "".join(head) + body, attach_parts


def convert(eml_path: Path, pdf_path: Path, save_attachments: bool = False,
            with_headers: bool = True) -> tuple[int, int]:
    raw = eml_path.read_bytes()
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    with tempfile.TemporaryDirectory(prefix="eml2pdf_") as td:
        workdir = Path(td)
        doc_html, attach_parts = build_html(msg, workdir)
        if with_headers:
            doc_html += headers_html(msg)

        story = fitz.Story(html=doc_html, archive=fitz.Archive(str(workdir)))
        writer = fitz.DocumentWriter(str(pdf_path))
        mediabox = fitz.paper_rect("letter")
        where = mediabox + (54, 54, -54, -54)

        pages, more = 0, 1
        while more:
            dev = writer.begin_page(mediabox)
            more, _ = story.place(where)
            story.draw(dev)
            writer.end_page()
            pages += 1
            if pages > 400:                     # runaway guard
                break
        writer.close()

    saved = 0
    if save_attachments and attach_parts:
        outdir = pdf_path.with_suffix("")
        outdir = outdir.parent / f"{outdir.name}_attachments"
        outdir.mkdir(exist_ok=True)
        for p in attach_parts:
            name = os.path.basename(dec(p.get_filename() or "attachment.bin"))
            try:
                data = p.get_payload(decode=True)
                if data:
                    (outdir / name).write_bytes(data)
                    saved += 1
            except Exception:
                pass
    return pages, saved


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    save_att = "--save-attachments" in sys.argv
    with_headers = "--no-headers" not in sys.argv
    if not args:
        sys.exit(__doc__)

    target = Path(args[0])
    if target.is_dir():
        emls = sorted(target.glob("*.eml"))
        if not emls:
            sys.exit(f"No .eml files in {target}")
        for e in emls:
            try:
                pages, saved = convert(e, e.with_suffix(".pdf"), save_att, with_headers)
                extra = f", {saved} attachment(s)" if saved else ""
                print(f"  {e.name}  ->  {e.with_suffix('.pdf').name}  ({pages} page(s){extra})")
            except Exception as ex:
                print(f"  !! {e.name}: {ex}")
        return

    if not target.exists():
        sys.exit(f"Not found: {target}")
    out = Path(args[1]) if len(args) > 1 else target.with_suffix(".pdf")
    pages, saved = convert(target, out, save_att, with_headers)
    extra = f", {saved} attachment(s) saved" if saved else ""
    print(f"{target.name} -> {out}  ({pages} page(s){extra})")


if __name__ == "__main__":
    main()
