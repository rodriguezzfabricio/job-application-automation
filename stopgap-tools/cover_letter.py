#!/usr/bin/env python3
"""Render a letter using the private profile and an approved body."""
import datetime, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
from runtime_paths import PRIVATE
OUT = os.path.join(PRIVATE, "cover-letters")
CONFIG = os.path.join(PRIVATE, "runtime-config.json")


def main(argv):
    if len(argv) != 4:
        print(__doc__)
        return 2
    company, title, body_path = argv[1], argv[2], argv[3]
    try:
        with open(CONFIG, encoding="utf-8") as stream:
            config = json.load(stream)
        name, contact = config["name"], config["contact"]
        with open(body_path, encoding="utf-8") as stream:
            body = stream.read().strip()
        words = len(body.split())
        if not 60 <= words <= 200:
            raise ValueError(f"body has {words} words; it must be 60-200")
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
        os.makedirs(OUT, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9]+", "_", company).strip("_")[:40] or "Company"
        path = os.path.join(OUT, f"Cover_Letter_{slug}_{__import__('hashlib').sha256((title + str(os.path.abspath(body_path))).encode()).hexdigest()[:12]}.pdf")
        st = ParagraphStyle("b", fontName="Times-Roman", fontSize=11.5, leading=15)
        head = ParagraphStyle("h", fontName="Times-Bold", fontSize=15, leading=19)
        esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        story = [Paragraph(esc(name), head), Paragraph(esc(contact), st), Spacer(1, 16),
                 Paragraph(datetime.date.today().strftime("%B %d, %Y"), st), Spacer(1, 12),
                 Paragraph(esc(f"Re: {title}"), st), Spacer(1, 12),
                 Paragraph(esc(f"Dear {company} hiring team,"), st), Spacer(1, 10)]
        for para in [p.strip() for p in body.split("\n\n") if p.strip()]:
            story += [Paragraph(esc(" ".join(para.split())), st), Spacer(1, 10)]
        story += [Paragraph("Sincerely,", st), Paragraph(esc(name), st)]
        SimpleDocTemplate(path, pagesize=letter, leftMargin=72, rightMargin=72, topMargin=72, bottomMargin=72,
                          title=f"Cover letter - {company}", author=name).build(story)
        print(json.dumps({"ok": True, "path": path, "words": words}))
        return 0
    except Exception as e:  # noqa: BLE001 - one JSON line back to the stage either way
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
