# json_to_csv.py
import json
import sys
import pandas as pd

src = sys.argv[1]
dst = sys.argv[2] if len(sys.argv) > 2 else src.rsplit('.', 1)[0] + '.csv'

with open(src, 'r', encoding='utf-8') as f:
    data = json.load(f)

pd.json_normalize(data).to_csv(dst, index=False)

print(f'Wrote {dst}')