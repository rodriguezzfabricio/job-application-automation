"""Pack a read-only browser form projection for the supported host Playwright surface."""
from pathlib import Path

SOURCE = Path(__file__).resolve().parent


def reader():
    """Use like cards_js: tab.playwright.locator(page_selector).evaluateAll(eval(page_js))."""
    declarations = []
    for name in ('browser_form_read_helpers.mjs', 'browser_form_reader.mjs'):
        source = (SOURCE / name).read_text(encoding='utf-8')
        declarations.append(source.replace('export ', '', 1))
    return {
        'page_selector': 'body',
        'submit_js': '(() => {' + (SOURCE/'browser_submit.mjs').read_text().replace('export async function ', 'async function ') + '\nreturn clickWithinGrant; })()',
        'operation_js': (SOURCE/'browser_form_operation.mjs').read_text().replace('export async function runFormStep','async function',1),
        'page_js': 'elements => {\n' + '\n'.join(declarations) + '\nreturn inspectForm(elements);\n}',
    }
