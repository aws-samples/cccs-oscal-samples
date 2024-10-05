#!/bin/bash

# Define the list of profiles
profiles=("mods" "medium" "pbhva")

# Loop through each profile
for profile in "${profiles[@]}"; do
    echo "Processing $profile profile..."

    # Resolve the profile to a catalog
    oscal-cli profile resolve --to=json --overwrite -q cccs-$profile-profile.json cccs-$profile-resolved.json

    # Validate the resolved catalog
    oscal-cli validate cccs-$profile-resolved.json

    # Convert to CSV
    python scripts/catalog-to-csv.py cccs-$profile-resolved.json cccs-$profile-resolved.csv
    echo "CSV file generated: cccs-$profile-resolved.csv"

    echo "Finished processing $profile profile."
    echo
done

echo "All profiles resolved successfully."
