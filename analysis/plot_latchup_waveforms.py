#!/usr/bin/env python3
# %%
import pandas as pd
import matplotlib.pyplot as plt
import argparse

filename = None

parser = argparse.ArgumentParser()
parser.add_argument("filename", type=str, help="Name of the file to read from")
args = parser.parse_args()
filename = args.filename

#filename = "../run_081/latch_data.pkl"


#%%

#filename = "../run_109/latch_data.pkl"

df = pd.read_pickle(filename)
#plt.plot(df)
offset = 0
for column in df.columns:
    plt.plot(df.index, df[column] + offset, label=column)
    offset += 0  # Increase the offset for the next column


# Put a legend to the right of the current axis
plt.legend(loc='center left', bbox_to_anchor=(1, 0.5))
plt.title(f"Run data {args.filename}, {len(df.columns)} latch-ups observed")
plt.show()

print(df.to_string())

# %%
