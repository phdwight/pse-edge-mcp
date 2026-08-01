"""Render a markdown doc to print-styled HTML, for PDF export.

    uv run --with markdown python scripts/render_doc_pdf.py docs/walkthrough.md out.html
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" --headless \
      --no-pdf-header-footer --print-to-pdf=docs/walkthrough.pdf "file://$PWD/out.html"

Any Chromium (Edge, Chrome, Brave) prints it, so pandoc and a LaTeX toolchain are not
required. That is deliberate: the diagrams are ASCII inside <pre>, so they survive any
renderer that honours a monospace font, and the .md stays readable on GitHub unchanged.
"""

import sys

import markdown

src, out_html = sys.argv[1], sys.argv[2]
text = open(src).read()

body = markdown.markdown(
    text,
    extensions=["fenced_code", "tables", "toc", "attr_list", "sane_lists"],
    extension_configs={"toc": {"permalink": False}},
)

CSS = """
@page { size: A4; margin: 16mm 14mm 16mm 14mm; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
       font-size: 9.6pt; line-height: 1.5; color: #1a1a1a; margin: 0; }
h1 { font-size: 20pt; margin: 0 0 2mm; letter-spacing: -0.4pt;
     border-bottom: 2.5px solid #1a1a1a; padding-bottom: 2mm; }
h2 { font-size: 13pt; margin: 8mm 0 2.5mm; padding-top: 2mm;
     border-top: 1px solid #d4d4d4; letter-spacing: -0.2pt;
     page-break-after: avoid; break-after: avoid; }
h3 { font-size: 10.5pt; margin: 5mm 0 1.5mm; color: #000;
     page-break-after: avoid; break-after: avoid; }
h2 + p, h3 + p { margin-top: 0; }
p { margin: 0 0 2.5mm; }
code { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 8.4pt;
       background: #f2f2f0; padding: 0.4mm 1mm; border-radius: 2px; }
pre { background: #f7f7f5; border: 1px solid #e2e2de; border-left: 3px solid #999;
      border-radius: 3px; padding: 2.5mm 3mm; overflow: visible;
      page-break-inside: avoid; break-inside: avoid; margin: 0 0 3mm; }
pre code { background: none; padding: 0; font-size: 7.5pt; line-height: 1.32;
           white-space: pre; }
table { border-collapse: collapse; width: 100%; margin: 0 0 3.5mm; font-size: 8.5pt;
        page-break-inside: avoid; break-inside: avoid; }
th { background: #efefec; text-align: left; font-weight: 600;
     border: 1px solid #d4d4d4; padding: 1.3mm 2mm; }
td { border: 1px solid #e0e0e0; padding: 1.3mm 2mm; vertical-align: top; }
tr:nth-child(even) td { background: #fafaf8; }
blockquote { margin: 0 0 3mm; padding: 2mm 3mm; background: #fdf9ec;
             border-left: 3px solid #d9a441; page-break-inside: avoid; }
blockquote p:last-child { margin-bottom: 0; }
ul, ol { margin: 0 0 3mm; padding-left: 5.5mm; }
li { margin-bottom: 1mm; }
hr { border: 0; border-top: 1px solid #d4d4d4; margin: 6mm 0; }
a { color: #1a4f8a; text-decoration: none; }
strong { font-weight: 600; }
"""

open(out_html, "w").write(
    f"<!doctype html><html><head><meta charset='utf-8'>"
    f"<title>pse-edge-mcp Developer Walkthrough</title>"
    f"<style>{CSS}</style></head><body>{body}</body></html>"
)
print("html written:", out_html)
