import csv
import json
import re
import sys

def process_control(control, parent_params=[], sibling_params=[]):
    props = {p['name']: p['value'] for p in control.get('props')}

    # Skip withdrawn controls
    if 'status' in props and props['status'] == 'withdrawn':
        return

    control_id = control['id'].upper()
    control_sort_id = props['sort-id']
    statement = next(part for part in control['parts'] if part['name'] == 'statement')
    control_params = control.get('params', [])
    all_params = parent_params + sibling_params + control_params
    guidance = next(part for part in control['parts'] if part['name'] == 'guidance')

    def process_statement_to_row(statement, params, first_row=False):
        if 'smt.' in statement['id']:
            statement_id = statement['id'].split('smt.', 1)[-1].upper()
        else:
            statement_id = ''

        row_sort_id = f'{control_sort_id}-{statement_id}'
        prose = statement.get('prose', '')

        def replace_param(param_id):
            param = next(p for p in params if p['id'] == param_id)
            if 'values' in param:
                return f'[CCCS Assignment: {param['values'][0]}]'
            elif 'select' in param:
                return f'[Selection: {', '.join(param['select']['choice'])}]'
            elif 'props' in param:
                for prop in param['props']:
                    if prop['name'] == 'alt-label':
                        return f'[Assignment: organization-defined {prop['value']}]'

            return f'[Assignment: organization-defined {param['label']}]'

        for n in range(2): # Repeat to handle nested params
            for match in re.findall(r'{{ insert\: param\, (.*?) }}', prose):
                prose = prose.replace(f'{{{{ insert: param, {match} }}}}', replace_param(match))

        # Add guidance to first row of control
        row_guidance = guidance.get('prose', '') if first_row else ''

        csv_rows.append([row_sort_id, control_id, statement_id, prose, row_guidance])

        # Recurse for nested statements
        if 'parts' in statement:
            for sub_statement in statement['parts']:
                process_statement_to_row(sub_statement, params)

    process_statement_to_row(statement, all_params, first_row=True)

    # Recurse for nested controls
    if 'controls' in control:
        nested_sibling_params = []
        for sub_control in control['controls']:
            nested_sibling_params.extend(sub_control.get('params', []))
        
        for sub_control in control['controls']:
            process_control(sub_control, parent_params=all_params, sibling_params=nested_sibling_params)

# Main
if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python catalog-to-csv.py <catalog_file_path> <csv_output_path>")
        sys.exit(1)

    catalog_path = sys.argv[1]
    csv_output_path = sys.argv[2]

    # Read catalog
    with open(catalog_path, 'r') as catalog_file:
        catalog = json.load(catalog_file)['catalog']

    # Collect CSV rows
    csv_rows = []

    # Process grouped controls
    if 'groups' in catalog:
        for group in catalog['groups']:

            # Collect parameters
            group_sibling_params = []
            for control in group['controls']:
                group_sibling_params.extend(control.get('params', []))
            
            # Process controls
            for control in group['controls']:
                process_control(control, sibling_params=group_sibling_params)

    # Process ungrouped controls
    if 'controls' in catalog:
        for control in catalog['controls']:
            process_control(control)

    # Sort rows by sort-id
    csv_rows.sort(key=lambda x: x[0])

    # Write CSV file
    with open(csv_output_path, 'w', newline='') as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(['Row Sort ID', 'Control ID', 'Statement ID', 'Control Statement', 'Supplemental Guidance'])
        writer.writerows(csv_rows)