#!/usr/bin/env python3
"""Render a paper-like HTML document into a ~3 page PDF for PaperDesk tests.

Usage: python3 tests/make_sample_pdf.py OUT.pdf
Requires the `playwright` python package and Chrome at /usr/bin/google-chrome
(falls back to playwright's bundled chromium).
"""
import os
import sys

PARA = (
    "The dominant sequence transduction models are based on complex recurrent or convolutional "
    "neural networks that include an encoder and a decoder. The best performing models also connect "
    "the encoder and decoder through an attention mechanism. We propose a new simple network "
    "architecture, the Transformer, based solely on attention mechanisms, dispensing with recurrence "
    "and convolutions entirely. Experiments on two machine translation tasks show these models to be "
    "superior in quality while being more parallelizable and requiring significantly less time to train."
)

SECTIONS = [
    ("1 Introduction", 4),
    ("2 Background", 3),
    ("3 Model Architecture", 3),
    ("3.1 Scaled Dot-Product Attention", 2),
    ("4 Experiments", 4),
    ("5 Conclusion", 3),
]

HTML_HEAD = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Sample Paper</title>
<style>
 @page { size: Letter; margin: 0.9in 0.9in; }
 body { font-family: 'Times New Roman', 'DejaVu Serif', serif; font-size: 11pt; line-height: 1.45; color: #111; }
 h1 { font-size: 18pt; text-align: center; margin: 0 0 6pt; }
 .authors { text-align: center; font-size: 11pt; margin-bottom: 14pt; }
 .abstract { margin: 0 0.4in 14pt; font-size: 10pt; }
 .abstract b { display: block; text-align: center; margin-bottom: 4pt; }
 h2 { font-size: 12.5pt; margin: 14pt 0 5pt; }
 p { text-align: justify; margin: 0 0 7pt; }
 .eq { text-align: center; font-style: italic; margin: 10pt 0; font-size: 12pt; }
 table { border-collapse: collapse; margin: 10pt auto; font-size: 10pt; }
 th, td { border-top: 1px solid #000; border-bottom: 1px solid #000; padding: 3pt 10pt; }
 caption { caption-side: bottom; font-size: 9.5pt; padding-top: 4pt; }
</style></head><body>
<h1>Attention Is a Sample Paper for PaperDesk Testing</h1>
<div class="authors">Ada Lovelace<sup>1</sup>, Alan Turing<sup>2</sup><br>
<sup>1</sup>Analytical Engine Lab &nbsp; <sup>2</sup>Bletchley Institute</div>
<div class="abstract"><b>Abstract</b>"""


def build_html():
    parts = [HTML_HEAD, PARA, "</div>"]
    for title, n in SECTIONS:
        parts.append(f"<h2>{title}</h2>")
        for k in range(n):
            parts.append(f"<p>{PARA} (Paragraph {k + 1} of section {title.split()[0]}.)</p>")
        if title.startswith("3.1"):
            parts.append('<div class="eq">Attention(Q, K, V) = softmax(QK<sup>T</sup> / &radic;d<sub>k</sub>) V'
                         '&nbsp;&nbsp;&nbsp;&nbsp;(1)</div>')
            parts.append("<p>We suspect that for large values of d_k, the dot products grow large in magnitude, "
                         "pushing the softmax function into regions where it has extremely small gradients.</p>")
        if title.startswith("4"):
            parts.append("<table><tr><th>Model</th><th>BLEU (EN-DE)</th><th>BLEU (EN-FR)</th><th>Training cost</th></tr>"
                         "<tr><td>ByteNet</td><td>23.75</td><td>&mdash;</td><td>&mdash;</td></tr>"
                         "<tr><td>ConvS2S</td><td>25.16</td><td>40.46</td><td>9.6e18</td></tr>"
                         "<tr><td>Transformer (base)</td><td>27.3</td><td>38.1</td><td>3.3e18</td></tr>"
                         "<caption>Table 1: Sample results table.</caption></table>")
    parts.append("</body></html>")
    return "".join(parts)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    out = os.path.abspath(sys.argv[1])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        kwargs = {"headless": True}
        if os.path.exists("/usr/bin/google-chrome"):
            kwargs["executable_path"] = "/usr/bin/google-chrome"
        browser = p.chromium.launch(**kwargs)
        page = browser.new_page()
        page.set_content(build_html(), wait_until="load")
        page.pdf(path=out, format="Letter", print_background=True)
        browser.close()
    print(out)


if __name__ == "__main__":
    main()
