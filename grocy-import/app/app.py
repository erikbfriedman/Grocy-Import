import base64
import io
import json
import os
import re

import requests
from flask import Flask, render_template, request, jsonify

try:
    import anthropic as _anthropic_lib
    _HAS_CLAUDE = True
except ImportError:
    _HAS_CLAUDE = False

app = Flask(__name__)

GROCY_URL         = os.environ.get('GROCY_URL', '').rstrip('/')
GROCY_API_KEY     = os.environ.get('GROCY_API_KEY', '')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

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
        'min_stock_amount',
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


@app.route('/api/receipt/status')
def receipt_status():
    return jsonify({
        'claude_ready':       _HAS_CLAUDE and bool(ANTHROPIC_API_KEY),
        'anthropic_package':  _HAS_CLAUDE,
        'api_key_configured': bool(ANTHROPIC_API_KEY),
    })


@app.route('/api/receipt/parse', methods=['POST'])
def parse_receipt():
    if not _HAS_CLAUDE:
        return jsonify({'error': 'anthropic package not installed'}), 500
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'Claude API key not configured — add it in the add-on Configuration tab'}), 400
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    raw    = request.files['file'].read()
    pdf_b64 = base64.standard_b64encode(raw).decode('utf-8')

    prompt = (
        'Extract all purchased items from this Walmart receipt. '
        'Return ONLY valid JSON, no other text:\n'
        '{"store":"City, State","date":"MM/DD/YY","total":0.00,'
        '"items":[{"description":"NAME","price":0.00,"quantity":1,"barcode":"012345678901"}]}\n\n'
        'Rules:\n'
        '- description: name as printed on receipt\n'
        '- price: amount paid after discounts/rollbacks\n'
        '- quantity: units purchased (default 1)\n'
        '- barcode: UPC/item number if visible, else null\n'
        '- Exclude taxes, totals, coupons, payments, fees — purchased items only'
    )

    try:
        client = _anthropic_lib.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model='claude-3-5-sonnet-20241022',
            max_tokens=4096,
            messages=[{
                'role': 'user',
                'content': [
                    {
                        'type': 'document',
                        'source': {
                            'type': 'base64',
                            'media_type': 'application/pdf',
                            'data': pdf_b64,
                        },
                    },
                    {'type': 'text', 'text': prompt},
                ],
            }],
        )
        raw_text = msg.content[0].text.strip()
        if raw_text.startswith('```'):
            raw_text = '\n'.join(raw_text.split('\n')[1:]).rstrip('`').strip()
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        return jsonify({'error': f'Could not parse receipt structure: {e}'}), 500
    except Exception as e:
        return jsonify({'error': f'Receipt parsing error: {e}'}), 500

    items = []
    for item in data.get('items', []):
        items.append({
            'description': str(item.get('description', '')).strip(),
            'price':       float(item.get('price', 0) or 0),
            'quantity':    int(item.get('quantity', 1) or 1),
            'barcode':     str(item['barcode']) if item.get('barcode') else None,
        })

    return jsonify({
        'store': str(data.get('store', '')),
        'date':  str(data.get('date', '')),
        'total': float(data.get('total', 0) or 0),
        'items': items,
    })


_RECEIPT_TEXT_JUNK = re.compile(
    r'^(?:'
    r'\d+\s+items?'                      # "38 items"
    r'|Substitutions?(?:\s*\(.*?\))?'    # "Substitution", "Substitutions(2 items)"
    r'|Weight-adjusted'
    r'|Shopped'
    r'|(?:Multipack Quantity|Scent|Flavor|Size|Total Count|Count Per Pack|'
    r'Count|Number of Sheets|Actual Color|Clothing Size|Color|Style|Type|'
    r'Pattern|Material|Pack|Gender|Brand)\s*:.*'   # "Scent: Lavender", "Flavor: Loaded Taco"
    r'|\d+\.?\d*[¢c]/\S.*'               # "17.3¢/fl oz" per-unit price
    r'|\$[\d.]+/(?:lb|fl\s*oz|oz|ct)'    # "$1.87/lb" per-unit price
    r'|Was\s+\$[\d.]+'                   # "Was $3.50" (pre-discount price)
    r'|\$[\d.]+\s+ea'                    # "$3.86 ea"
    r'|\$[\d.]+\s+from\s+savings'        # "$0.53 from savings"
    r'|Walmart\s+Cash\b.*'               # "Walmart Cash Logo", "Walmart Cash added"
    r'|Final\s+weight\b.*'               # "Final weight 2.6 lbs"
    r')$',
    re.IGNORECASE,
)


@app.route('/api/receipt/parse-text', methods=['POST'])
def parse_receipt_text():
    """
    Parse copy-pasted Walmart online order text.

    Each item block (delimited by "Add to cart" ... "Review item"/"Write a
    review") contains the product name, optional attribute/pricing noise
    lines, then a "Qty N" line immediately followed by the line-total price:
        Product name (one or more lines)
        [Scent: ... / Multipack Quantity: ... / per-unit price / etc.]
        Qty N  |  Wt X.XX lb
        $X.XX
        [Was $X.XX / $X.XX ea / Walmart Cash ... ]
    """
    text = (request.json or {}).get('text', '').strip()
    if not text:
        return jsonify({'error': 'No text provided'}), 400

    items = []
    # Split on the "Add to cart … Review item" (or legacy "Write a review") delimiter
    blocks = re.split(
        r'Add\s+to\s+cart[\s\S]*?(?:Review\s+item|Write\s+a\s+review)',
        text,
        flags=re.IGNORECASE,
    )

    for block in blocks:
        lines = [
            l.strip() for l in block.split('\n')
            if l.strip() and not _RECEIPT_TEXT_JUNK.match(l.strip())
        ]
        if len(lines) < 2:
            continue

        # Find the "Qty N" or "Wt X.XX lb" line — the line right after it is the price
        qty, qty_idx = 1, None
        for i, line in enumerate(lines):
            m = re.match(r'^Qty\s+(\d+)$', line, re.I)
            if m:
                qty, qty_idx = int(m.group(1)), i
                break
            if re.match(r'^Wt\s+[\d.]+\s*lb', line, re.I):
                qty, qty_idx = 1, i
                break

        if qty_idx is None or qty_idx + 1 >= len(lines):
            continue

        price_m = re.match(r'^\$([\d.]+)$', lines[qty_idx + 1])
        if not price_m:
            continue
        price = float(price_m.group(1))

        name = ' '.join(lines[:qty_idx]).strip()
        if name and len(name) > 3:
            items.append({
                'description': name,
                'price':       price,
                'quantity':    qty,
                'barcode':     None,
            })

    if not items:
        return jsonify({'error': 'No items found — make sure to copy the full order list including "Add to cart / Review item" lines'}), 400

    total = round(sum(i['price'] for i in items), 2)
    return jsonify({'store': '', 'date': '', 'total': total, 'items': items, 'source': 'order_text'})


@app.route('/api/receipt/decode', methods=['POST'])
def decode_receipt():
    """Decode Walmart abbreviations with Claude, then barcode-match against Grocy."""
    if not _HAS_CLAUDE:
        return jsonify({'error': 'anthropic package not installed'}), 500
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'Claude API key not configured'}), 400

    items = (request.json or {}).get('items', [])
    if not items:
        return jsonify({'items': []}), 200

    descriptions = [str(i.get('description', '')) for i in items]
    desc_list    = '\n'.join(f'{n + 1}. {d}' for n, d in enumerate(descriptions))

    try:
        client = _anthropic_lib.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model='claude-3-5-haiku-20241022',
            max_tokens=2048,
            messages=[{
                'role': 'user',
                'content': (
                    'These are Walmart receipt item names (possibly abbreviated). '
                    'Return ONLY a JSON array of decoded product names — one string per item, same order.\n'
                    'Example: ["Apple Juice 64oz","Bananas 3lb","Tide Pods 57ct"]\n\n'
                    f'Items:\n{desc_list}'
                ),
            }],
        )
        raw_text = msg.content[0].text.strip()
        if raw_text.startswith('```'):
            raw_text = '\n'.join(raw_text.split('\n')[1:]).rstrip('`').strip()
        decoded = json.loads(raw_text)
        if not isinstance(decoded, list):
            decoded = descriptions
    except Exception:
        decoded = descriptions

    result_items = []
    for i, item in enumerate(items):
        decoded_name = str(decoded[i]) if i < len(decoded) else item.get('description', '')
        updated = {**item, 'decoded_name': decoded_name, 'grocy_match': None, 'status': 'new'}

        # Barcode lookup against Grocy
        barcode = item.get('barcode')
        if barcode and GROCY_URL and GROCY_API_KEY:
            try:
                r = requests.get(
                    f'{GROCY_URL}/api/stock/products/by-barcode/{barcode}',
                    headers=grocy_headers(),
                    timeout=5,
                )
                if r.ok:
                    d = r.json()
                    product = d.get('product') if isinstance(d, dict) else None
                    if product and product.get('id'):
                        updated['grocy_match'] = {
                            'id':   str(product['id']),
                            'name': product.get('name', 'Unknown'),
                        }
                        updated['status'] = 'matched'
            except Exception:
                pass

        result_items.append(updated)

    return jsonify({'items': result_items})


@app.route('/api/stock/entries')
def stock_entries():
    location_id = request.args.get('location_id')
    params = {}
    if location_id:
        params['query[]'] = f'location_id={location_id}'
    try:
        entries_r  = requests.get(f'{GROCY_URL}/api/objects/stock', headers=grocy_headers(), params=params, timeout=10)
        products_r = requests.get(f'{GROCY_URL}/api/objects/products', headers=grocy_headers(), timeout=10)
        entries  = entries_r.json()  if entries_r.ok  else []
        products = {str(p['id']): p.get('name', '') for p in (products_r.json() if products_r.ok else [])}
        qu_r = requests.get(f'{GROCY_URL}/api/objects/quantity_units', headers=grocy_headers(), timeout=10)
        qus  = {str(q['id']): q.get('name', '') for q in (qu_r.json() if qu_r.ok else [])}
        for e in entries:
            e['product_name'] = products.get(str(e.get('product_id', '')), 'Unknown')
            e['qu_name']      = qus.get(str(e.get('qu_id', '')), '')
        return jsonify(entries)
    except Exception as ex:
        return jsonify({'error': str(ex)}), 500


@app.route('/api/stock/entries/<int:entry_id>', methods=['DELETE'])
def delete_stock_entry(entry_id):
    try:
        r = requests.delete(f'{GROCY_URL}/api/objects/stock/{entry_id}', headers=grocy_headers(), timeout=10)
        return jsonify({'ok': r.ok, 'status': r.status_code})
    except Exception as ex:
        return jsonify({'ok': False, 'error': str(ex)}), 500


@app.route('/api/stock/correct', methods=['POST'])
def stock_correct():
    corrections = (request.json or {}).get('corrections', [])
    results = []
    for c in corrections:
        product_id = c.get('product_id')
        if not product_id:
            continue
        body = {
            'new_amount':       float(c.get('new_amount', 0)),
            'best_before_date': c.get('best_before_date') or '2999-12-31',
            'location_id':      c.get('location_id'),
        }
        try:
            r = requests.post(
                f'{GROCY_URL}/api/stock/products/{product_id}/inventory',
                headers=grocy_headers(), json=body, timeout=10,
            )
            results.append({'product_id': product_id, 'ok': r.ok, 'status': r.status_code})
        except Exception as ex:
            results.append({'product_id': product_id, 'ok': False, 'error': str(ex)})
    return jsonify({'results': results})


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


# ── Meal Planner ─────────────────────────────────────────────────────────────

_MP_ENTITIES = {
    'mp_users': {
        'caption': 'Meal Plan Users',
        'fields': [
            ('name',          'Name',             'text_short'),
            ('color',         'Color',             'text_short'),
            ('calorie_goal',  'Calorie Goal',      'number'),
            ('protein_goal',  'Protein Goal g',    'number'),
            ('carbs_goal',    'Carbs Goal g',      'number'),
            ('fat_goal',      'Fat Goal g',        'number'),
        ],
    },
    'mp_categories': {
        'caption': 'Meal Categories',
        'fields': [
            ('name',       'Name',       'text_short'),
            ('sort_order', 'Sort Order', 'number'),
        ],
    },
    'mp_meals': {
        'caption': 'Meal Plan Entries',
        'fields': [
            ('plan_date',    'Date',        'date'),
            ('category_id',  'Category ID', 'number'),
            ('recipe_name',  'Recipe Name', 'text_short'),
            ('notes',        'Notes',       'text_long'),
        ],
    },
    'mp_meal_users': {
        'caption': 'Meal Participants',
        'fields': [
            ('meal_id',  'Meal ID',    'number'),
            ('user_id',  'User ID',    'number'),
            ('servings', 'Servings',   'number'),
            ('calories', 'Calories',   'number'),
            ('protein',  'Protein g',  'number'),
            ('carbs',    'Carbs g',    'number'),
            ('fat',      'Fat g',      'number'),
        ],
    },
}

_MP_DEFAULT_CATS = [('Breakfast', 1), ('Lunch', 2), ('Dinner', 3), ('Snack', 4)]


@app.route('/api/grocy-proxy/<path:gpath>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
def grocy_proxy(gpath):
    """Generic proxy to the Grocy REST API (used by Meal Planner)."""
    target = f'{GROCY_URL}/api/{gpath}'
    try:
        kwargs = {'headers': grocy_headers(), 'timeout': 15}
        if request.args:
            kwargs['params'] = request.args.to_dict(flat=False)
        if request.method in ('POST', 'PUT', 'PATCH') and request.data:
            kwargs['json'] = request.get_json(silent=True) or {}
        resp = requests.request(request.method, target, **kwargs)
        try:
            data = resp.json()
        except Exception:
            data = resp.text or ''
        return jsonify(data), resp.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/meal-planner/setup', methods=['POST'])
def meal_planner_setup():
    """Create Grocy userentities + userfields for the meal planner."""
    log, errors = [], []

    def _post(path, body):
        return requests.post(f'{GROCY_URL}/api/{path}', headers=grocy_headers(), json=body, timeout=10)

    for ename, cfg in _MP_ENTITIES.items():
        try:
            chk = requests.get(f'{GROCY_URL}/api/objects/{ename}', headers=grocy_headers(), timeout=10)
            if chk.status_code != 200:
                r = _post('userentities', {'name': ename, 'caption': cfg['caption'],
                                           'description': '', 'show_in_sidebar_menu': 0, 'icon_css_class': ''})
                (log if r.ok else errors).append(f'{"Created" if r.ok else "FAILED"} entity {ename}')
            else:
                log.append(f'Entity exists: {ename}')
        except Exception as e:
            errors.append(f'Entity {ename}: {e}'); continue

        for fname, fcaption, ftype in cfg['fields']:
            try:
                r = _post('userfields', {'entity': f'userentity-{ename}', 'name': fname,
                                         'caption': fcaption, 'type': ftype,
                                         'show_as_column_in_tables': 1, 'config': '{}'})
                if not r.ok and r.status_code != 409:
                    errors.append(f'Field {ename}.{fname}: {r.text[:80]}')
            except Exception as e:
                errors.append(f'Field {ename}.{fname}: {e}')

    try:
        cats_r = requests.get(f'{GROCY_URL}/api/objects/mp_categories', headers=grocy_headers(), timeout=10)
        if cats_r.ok and not (cats_r.json() or []):
            for name, order in _MP_DEFAULT_CATS:
                _post('objects/mp_categories', {'name': name, 'sort_order': str(order)})
            log.append('Seeded default categories')
    except Exception as e:
        errors.append(f'Category seed: {e}')

    return jsonify({'ok': not errors, 'log': log, 'errors': errors})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8099))
    app.run(host='0.0.0.0', port=port, debug=False)
