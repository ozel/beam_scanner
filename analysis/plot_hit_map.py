#!/usr/bin/env python3
import argparse

import pandas as pd
import matplotlib.pyplot as plt

parser = argparse.ArgumentParser()
parser.add_argument("filename", type=str, help="Name of the file to read from")
args = parser.parse_args()

data = pd.read_csv(args.filename)

plt.plot(data["x_lsb"], data["y_lsb"], 'k.', alpha=0.1, )
plt.xlabel("X position (LSB)")
plt.ylabel("Y position (LSB)")
plt.grid(visible=True, which='major', linestyle='-', color="black", linewidth='0.8')
plt.grid(visible=True, which='minor', linestyle='-', linewidth='.5')

plt.minorticks_on()

plt.show()
