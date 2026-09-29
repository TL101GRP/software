#!/bin/bash

# Run hotspot.py in the background
python hotspot.py &

# Run csi_plot.py in the background
python csi_plot.py &

# Wait for both background processes to finish before exiting the script
wait
