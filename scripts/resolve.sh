#!/bin/bash

# Define the list of profiles
profiles=("mods" "medium" "pbhva-overlay" "medium+pbhva" "itsp.10.171")

# Validate the CCCS catalog
echo "Validating CCCS catalog..."
oscal-cli validate cccs-control-catalog/cccs-control-catalog.json
echo "Finished validating CCCS catalog"
echo

# Loop through each profile
for profile in "${profiles[@]}"; do
    echo "Processing $profile profile..."

    # Change working directory to the profile
    cd "cccs-$profile-profile"

    # Resolve the profile to a catalog
    oscal-cli profile resolve --to=json --overwrite -q cccs-$profile-profile.json cccs-$profile-resolved.json

    # Validate the resolved catalog
    oscal-cli validate cccs-$profile-resolved.json

    # Trim file paths
    sed -i .bak 's|file:.*cccs-oscal-samples/||g' cccs-$profile-resolved.json
    rm cccs-$profile-resolved.json.bak
    
    # Convert to CSV
    python ../scripts/catalog-to-csv.py cccs-$profile-resolved.json cccs-$profile-resolved.csv
    echo "CSV file generated: cccs-$profile-resolved.csv"

    # Change back to the original directory
    cd ..

    echo "Finished processing $profile profile."
    echo
done

echo "All profiles resolved successfully."
