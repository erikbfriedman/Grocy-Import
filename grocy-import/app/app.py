import io
import os
import re

import requests
from flask import Flask, render_template, request, jsonify

try:
    import fitz          # pymupdf
    from PIL import Image
    import pytesseract
    _HAS_OCR = True
except ImportError:
    _HAS_OCR = False

# Walmart online order format: "Description [status] Qty N $price"
_WALMART_ONLINE_RE  = re.compile(r'^(.+?)\s+Qty\s+(\d+)\s+\$(\d+\.\d{2})\s*$')
# Status words to strip from the end of descriptions in online orders
_WALMART_STATUS_RE  = re.compile(
    r'\s+(?:Shopped|Weight[\s\w→\-]*Adjusted|Adjusted|Not\s+Available|Weight[-\s]adjusted)\s*$',
    re.IGNORECASE,
)
# In-store format: "<description>  <price> [optional single-letter tax code]"
_RECEIPT_PRICE_RE   = re.compile(r'^(.+?)\s{2,}(\d+\.\d{2})\s*[A-Z]?\s*$')
# In-store quantity prefix lines like "2 @ $1.98" or "3 X 0.89"
_RECEIPT_QTY_RE     = re.compile(r'^(\d+)\s*[@xX]\s*\$?(\d+\.\d{2})\s*$')
# Lines to skip in both formats
_RECEIPT_SKIP_RE    = re.compile(
    r'subtotal|tax|total|change due|cash|credit|debit|visa|mastercard|'
    r'discover|amex|savings|balance|rewards?|e-receipt|thank you|walmart|'
    r'store\s*#|\bTC\b|ebt|snap|wic|tender|amount due|amount paid|'
    r'items?\s+sold|approval|auth\s*#|ref\s*#|terminal|your cashier|'
    r'sc\s*#|transaction|phone|address|manager|driver\s*tip|delivery|'
    r'^\*+$|^order\s*#|^buyer|invoice',
    re.IGNORECASE,
)

app = Flask(__name__)

GROCY_URL = os.environ.get('GROCY_URL', '').rstrip('/')
GROCY_API_KEY = os.environ.get('GROCY_API_KEY', '')

ENTITIES = {
    'products': 'Products',
    'product_groups': 'Product Groups',
    'locations': 'Locations',
    'shopping_locations': 'Shopping Locations',
    'quantity_units': 'Quantity Units',
    'quantity_unit_conversions': 'Qty Unit Conversions',
    'chores': 'Chores',
    'tasks': 'Tasks',
    'task_categories': 'Task Categories',
    'batteries': 'Batteries',
    'shopping_list': 'Shopping List',
    'shopping_lists': 'Shopping Lists',
    'recipes': 'Recipes',
    'recipe_ingredients': 'Recipe Ingredients',
    'meal_plan': 'Meal Plan',
    'userfields': 'User Fields',
}

DEFAULT_COLUMNS = {
    'products': [
        'id', 'name', 'description', 'product_group_id', 'active',
        'location_id', 'shopping_location_id',
        'qu_id_purchase', 'qu_id_stock',
        'qu_factor_purchase_to_stock', 'min_stock_amount',
        'default_best_before_days', 'default_best_before_days_after_open',
        'default_best_before_days_after_freezing', 'default_best_before_days_after_thawing',
        'calories', 'enable_tare_weight_handling', 'tare_weight',
        'not_check_stock_fulfillment_for_recipes', 'parent_product_id',
    ],
    'product_groups': ['id', 'name', 'description'],
    'locations': ['id', 'name', 'description', 'is_freezer'],
    'shopping_locations': ['id', 'name', 'description'],
    'quantity_units': ['id', 'name', 'description', 'plural_forms'],
    'quantity_unit_conversions': ['id', 'from_qu_id', 'to_qu_id', 'factor', 'product_id'],
    'chores': [
        'id', 'name', 'description', 'period_type', 'period_days',
        'track_date_only', 'rollover', 'assignment_type',
        'next_execution_assigned_to_user_id', 'consume_product_on_execution',
        'product_id', 'product_amount', 'start_date',
    ],
    'tasks': ['id', 'name', 'description', 'due_date', 'assigned_to_user_id', 'category_id'],
    'task_categories': ['id', 'name', 'description'],
    'batteries': ['id', 'name', 'description', 'used_in', 'charge_interval_days'],
    'shopping_list': ['id', 'product_id', 'amount', 'qu_id', 'note', 'shopping_list_id'],
    'shopping_lists': ['id', 'name', 'description'],
    'recipes': [
        'id', 'name', 'description', 'base_servings', 'desired_servings',
        'not_check_shoppinglist', 'product_id', 'calories',
    ],
    'recipe_ingredients': [
        'id', 'recipe_id', 'product_id', 'amount', 'qu_id',
        'only_check_single_unit_in_stock', 'ingredient_group', 'note',
        'price_factor', 'variable_amount', 'not_check_stock_fulfillment',
    ],
    'meal_plan': [
        'id', 'day', 'recipe_id', 'servings', 'note',
        'product_id', 'product_amount', 'product_qu_id',
    ],
    'userfields': [
        'id', 'name', 'caption', 'entity', 'type',
        'show_as_column_in_tables', 'input_required', 'search_allowed',
    ],
}

# Columns Grocy returns in GET responses that are computed / not accepted in POST/PUT
NON_WRITABLE = frozenset({
    'userfields', 'row_created_timestamp',
})

# Explicit FK field → entity mappings for non-obvious names
LINKED_FIELDS = {
    'location_id':                        {'entity': 'locations',         'label': 'name'},
    'shopping_location_id':               {'entity': 'shopping_locations','label': 'name'},
    'product_group_id':                   {'entity': 'product_groups',    'label': 'name'},
    # Both naming conventions across Grocy versions
    'quantity_unit_id_purchase':          {'entity': 'quantity_units',    'label': 'name'},
    'quantity_unit_id_stock':             {'entity': 'quantity_units',    'label': 'name'},
    'qu_id_purchase':                     {'entity': 'quantity_units',    'label': 'name'},
    'qu_id_stock':                        {'entity': 'quantity_units',    'label': 'name'},
    'product_id':                         {'entity': 'products',          'label': 'name'},
    'parent_product_id':                  {'entity': 'products',          'label': 'name'},
    'qu_id':                              {'entity': 'quantity_units',    'label': 'name'},
    'recipe_id':                          {'entity': 'recipes',           'label': 'name'},
    'category_id':                        {'entity': 'task_categories',   'label': 'name'},
    'assigned_to_user_id':                {'entity': 'users',             'label': 'display_name'},
    'next_execution_assigned_to_user_id': {'entity': 'users',             'label': 'display_name'},
    'shopping_list_id':                   {'entity': 'shopping_lists',    'label': 'name'},
    'from_qu_id':                         {'entity': 'quantity_units',    'label': 'name'},
    'to_qu_id':                           {'entity': 'quantity_units',    'label': 'name'},
    'product_qu_id':                      {'entity': 'quantity_units',    'label': 'name'},
}


def grocy_headers():
    return {
        'GROCY-API-KEY': GROCY_API_KEY,
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }


def guess_entity_for_column(col_name):
    """
    Try to infer what entity an FK column references.

    Handles two patterns:
      - trailing _id:     parent_product_id → products
                          shopping_location_id → shopping_locations
      - embedded _id_:    qu_id_purchase → quantity_units
                          qu_id_stock    → quantity_units

    Strategy: extract the word(s) before _id (or before _id_*), then
    progressively strip leading words until we find a matching entity slug.
    """
    if col_name == 'id':
        return None

    # Extract the 'base' — the part that names the referenced entity
    if col_name.endswith('_id'):
        base = col_name[:-3]                        # strip trailing _id
    elif '_id_' in col_name:
        base = col_name[:col_name.index('_id_')]    # take the part before _id_
    else:
        return None

    parts = base.split('_')
    for i in range(len(parts)):
        sub = '_'.join(parts[i:])
        for candidate in (sub + 's', sub):
            if candidate in ENTITIES:
                label = 'display_name' if candidate == 'users' else 'name'
                return {'entity': candidate, 'label': label}

    return None


def fetch_entity_rows(entity):
    """Fetch all records for an entity (handles the special /api/users path)."""
    api_path = '/api/users' if entity == 'users' else f'/api/objects/{entity}'
    try:
        resp = requests.get(f"{GROCY_URL}{api_path}", headers=grocy_headers(), timeout=10)
        return resp.json() if resp.ok else []
    except Exception:
        return []


def build_source(rows, label_field):
    """Convert a list of Grocy records into [{id, name}] for jspreadsheet dropdowns."""
    return [
        {
            'id': str(row['id']),
            'name': str(row.get(label_field) or row.get('name') or row['id']),
        }
        for row in rows if row.get('id') is not None
    ]


@app.route('/')
def index():
    ingress_path = request.headers.get('X-Ingress-Path', '')
    return render_template('index.html', entities=ENTITIES, ingress_path=ingress_path)


@app.route('/api/config')
def get_config():
    return jsonify({
        'grocy_url': GROCY_URL,
        'configured': bool(GROCY_URL and GROCY_API_KEY),
    })


@app.route('/api/schema/<entity>')
def get_schema(entity):
    if entity not in ENTITIES:
        return jsonify({'error': 'Unknown entity'}), 400

    with_data = request.args.get('withData', '0') == '1'
    columns = list(DEFAULT_COLUMNS.get(entity, ['id', 'name']))
    data = []

    if GROCY_URL and GROCY_API_KEY:
        try:
            resp = requests.get(
                f"{GROCY_URL}/api/objects/{entity}",
                headers=grocy_headers(),
                timeout=10,
            )
            if resp.ok:
                fetched = resp.json() or []
                if fetched and isinstance(fetched, list):
                    sample = fetched[0]
                    actual_cols = [
                        c for c in sample.keys()
                        if c not in NON_WRITABLE
                        and not isinstance(sample.get(c), (dict, list))
                    ]
                    if 'id' in actual_cols:
                        actual_cols.remove('id')
                        actual_cols = ['id'] + actual_cols
                    columns = actual_cols
                if with_data:
                    data = fetched
        except Exception:
            pass

    return jsonify({'columns': columns, 'data': data})


@app.route('/api/lookups')
def get_lookups():
    """
    Returns dropdown source arrays keyed by field name.

    When ?entity=<slug> is provided, the endpoint also fetches that entity's
    live schema and auto-detects any _id columns not already in LINKED_FIELDS,
    so new FK columns added by Grocy upgrades are picked up automatically.
    """
    if not (GROCY_URL and GROCY_API_KEY):
        return jsonify({})

    # Start from the explicit map
    fields_to_resolve = dict(LINKED_FIELDS)

    # Auto-detect additional FK columns from the live schema of the requested entity
    entity_param = request.args.get('entity')
    if entity_param and entity_param in ENTITIES:
        try:
            resp = requests.get(
                f"{GROCY_URL}/api/objects/{entity_param}",
                headers=grocy_headers(),
                timeout=10,
            )
            if resp.ok:
                records = resp.json() or []
                if records and isinstance(records, list):
                    for col in records[0].keys():
                        if col not in fields_to_resolve and col not in NON_WRITABLE:
                            guessed = guess_entity_for_column(col)
                            if guessed:
                                fields_to_resolve[col] = guessed
        except Exception:
            pass

    # Fetch each unique referenced entity's records once
    entity_rows = {}
    for info in fields_to_resolve.values():
        ent = info['entity']
        if ent not in entity_rows:
            entity_rows[ent] = fetch_entity_rows(ent)

    # Build per-field source arrays
    field_sources = {}
    for field, info in fields_to_resolve.items():
        rows = entity_rows.get(info['entity'], [])
        if rows:
            field_sources[field] = build_source(rows, info['label'])

    return jsonify(field_sources)


@app.route('/api/import/<entity>', methods=['POST'])
def import_data(entity):
    if entity not in ENTITIES:
        return jsonify({'error': 'Unknown entity'}), 400

    rows = request.json.get('rows', [])
    results = []

    for row in rows:
        clean = {k: v for k, v in row.items() if v != '' and v is not None}
        row_id = clean.pop('id', None)

        try:
            if row_id:
                resp = requests.put(
                    f"{GROCY_URL}/api/objects/{entity}/{row_id}",
                    headers=grocy_headers(),
                    json=clean,
                    timeout=10,
                )
            else:
                resp = requests.post(
                    f"{GROCY_URL}/api/objects/{entity}",
                    headers=grocy_headers(),
                    json=clean,
                    timeout=10,
                )

            body = None
            if resp.content:
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text

            created_id = body.get('created_object_id') if isinstance(body, dict) else None

            results.append({
                'status': resp.status_code,
                'ok': resp.ok,
                'row': row,
                'response': body,
                'created_id': created_id,
            })
        except Exception as e:
            results.append({
                'status': 0,
                'ok': False,
                'row': row,
                'error': str(e),
            })

    return jsonify({'results': results})


@app.route('/api/receipt/parse', methods=['POST'])
def parse_receipt():
    if not _HAS_OCR:
        return jsonify({'error': 'OCR libraries not installed on server'}), 500
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    items = []
    try:
        raw = request.files['file'].read()
        doc = fitz.open(stream=raw, filetype='pdf')
        all_lines = []
        for page in doc:
            # Try native text extraction first (works for text-based PDFs)
            text = page.get_text('text')
            if not text.strip():
                # Fall back to OCR: render page at 3x zoom then run tesseract
                mat = fitz.Matrix(3, 3)
                pix = page.get_pixmap(matrix=mat)
                img = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
                text = pytesseract.image_to_string(img, config='--psm 6')
            all_lines.extend(text.split('\n'))

        # ── Walmart online order format ──────────────────────────────────────
        # Lines look like: "Description [status] Qty N $price"
        online_items = []
        for line in all_lines:
            line = line.strip()
            if not line or _RECEIPT_SKIP_RE.search(line):
                continue
            m = _WALMART_ONLINE_RE.match(line)
            if m:
                raw_desc = m.group(1).strip()
                desc     = _WALMART_STATUS_RE.sub('', raw_desc).strip()
                qty      = int(m.group(2))
                price    = float(m.group(3))
                if len(desc) > 2:
                    online_items.append({
                        'description': desc,
                        'price': price,
                        'quantity': qty,
                        'unit_price': round(price / qty, 2) if qty > 1 else price,
                    })

        if online_items:
            items = online_items
        else:
            # ── In-store receipt format fallback ─────────────────────────────
            i = 0
            while i < len(all_lines):
                line = all_lines[i].strip()
                if not line or _RECEIPT_SKIP_RE.search(line):
                    i += 1
                    continue

                qty_m = _RECEIPT_QTY_RE.match(line)
                if qty_m and i + 1 < len(all_lines):
                    nxt     = all_lines[i + 1].strip()
                    price_m = _RECEIPT_PRICE_RE.match(nxt)
                    if price_m and not _RECEIPT_SKIP_RE.search(nxt):
                        desc = price_m.group(1).strip()
                        if len(desc) > 2 and not re.match(r'^\d+$', desc):
                            items.append({
                                'description': desc,
                                'price': float(price_m.group(2)),
                                'quantity': int(qty_m.group(1)),
                                'unit_price': float(qty_m.group(2)),
                            })
                        i += 2
                        continue

                price_m = _RECEIPT_PRICE_RE.match(line)
                if price_m:
                    desc = price_m.group(1).strip()
                    if len(desc) > 2 and not _RECEIPT_SKIP_RE.search(desc) and not re.match(r'^\d+$', desc):
                        items.append({
                            'description': desc,
                            'price': float(price_m.group(2)),
                            'quantity': 1,
                            'unit_price': float(price_m.group(2)),
                        })
                i += 1

    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'items': items})


@app.route('/api/stock/receive', methods=['POST'])
def receive_stock():
    payload = request.json or {}
    results = []

    for item in payload.get('items', []):
        product_id = item.get('product_id')
        if not product_id:
            continue

        body = {
            'amount': item.get('amount', 1),
            'transaction_type': 'purchase',
        }
        if item.get('shopping_location_id'):
            body['shopping_location_id'] = item['shopping_location_id']

        try:
            resp = requests.post(
                f"{GROCY_URL}/api/stock/products/{product_id}/add",
                headers=grocy_headers(),
                json=body,
                timeout=10,
            )
            resp_body = None
            if resp.content:
                try:
                    resp_body = resp.json()
                except Exception:
                    resp_body = resp.text
            results.append({'product_id': product_id, 'ok': resp.ok,
                            'status': resp.status_code, 'response': resp_body})
        except Exception as e:
            results.append({'product_id': product_id, 'ok': False, 'error': str(e)})

    return jsonify({'results': results})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8099))
    app.run(host='0.0.0.0', port=port, debug=False)
