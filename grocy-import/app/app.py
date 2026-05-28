import os
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

GROCY_URL = os.environ.get('GROCY_URL', '').rstrip('/')
GROCY_API_KEY = os.environ.get('GROCY_API_KEY', '')

ENTITIES = {
    'products': 'Products',
    'product_groups': 'Product Groups',
    'locations': 'Locations',
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
        'location_id', 'quantity_unit_id_purchase', 'quantity_unit_id_stock',
        'quantity_unit_factor_purchase_to_stock', 'min_stock_amount',
        'default_best_before_days', 'default_best_before_days_after_open',
        'default_best_before_days_after_freezing', 'default_best_before_days_after_thawing',
        'calories', 'enable_tare_weight_handling', 'tare_weight',
        'not_check_stock_fulfillment_for_recipes',
    ],
    'product_groups': ['id', 'name', 'description'],
    'locations': ['id', 'name', 'description', 'is_freezer'],
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

# Maps FK field names to the entity they reference and which field to use as the display label
LINKED_FIELDS = {
    'location_id':                        {'entity': 'locations',       'label': 'name'},
    'product_group_id':                   {'entity': 'product_groups',  'label': 'name'},
    'quantity_unit_id_purchase':          {'entity': 'quantity_units',  'label': 'name'},
    'quantity_unit_id_stock':             {'entity': 'quantity_units',  'label': 'name'},
    'product_id':                         {'entity': 'products',        'label': 'name'},
    'qu_id':                              {'entity': 'quantity_units',  'label': 'name'},
    'recipe_id':                          {'entity': 'recipes',         'label': 'name'},
    'category_id':                        {'entity': 'task_categories', 'label': 'name'},
    'assigned_to_user_id':                {'entity': 'users',           'label': 'display_name'},
    'next_execution_assigned_to_user_id': {'entity': 'users',           'label': 'display_name'},
    'shopping_list_id':                   {'entity': 'shopping_lists',  'label': 'name'},
    'from_qu_id':                         {'entity': 'quantity_units',  'label': 'name'},
    'to_qu_id':                           {'entity': 'quantity_units',  'label': 'name'},
    'product_qu_id':                      {'entity': 'quantity_units',  'label': 'name'},
}


def grocy_headers():
    return {
        'GROCY-API-KEY': GROCY_API_KEY,
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }


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
                    actual_cols = list(fetched[0].keys())
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
    """Returns dropdown source arrays for all FK fields, keyed by field name."""
    if not (GROCY_URL and GROCY_API_KEY):
        return jsonify({})

    # Fetch each unique referenced entity once
    entity_rows = {}
    for info in LINKED_FIELDS.values():
        entity = info['entity']
        if entity in entity_rows:
            continue
        try:
            api_path = '/api/users' if entity == 'users' else f'/api/objects/{entity}'
            resp = requests.get(f"{GROCY_URL}{api_path}", headers=grocy_headers(), timeout=10)
            entity_rows[entity] = resp.json() if resp.ok else []
        except Exception:
            entity_rows[entity] = []

    # Build per-field source arrays: [{id: str, name: str}]
    field_sources = {}
    for field, info in LINKED_FIELDS.items():
        rows = entity_rows.get(info['entity'], [])
        if rows:
            field_sources[field] = [
                {
                    'id': str(row['id']),
                    'name': str(row.get(info['label']) or row.get('name') or row['id']),
                }
                for row in rows if row.get('id') is not None
            ]

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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8099))
    app.run(host='0.0.0.0', port=port, debug=False)
