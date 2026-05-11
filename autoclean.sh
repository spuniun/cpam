#!/bin/bash
#
###############################################################################
# Author            :  Louwrentius
# Contact           : louwrentius@gmail.com
# Initial release   : August 2011
# Licence           : Simplified BSD License
###############################################################################

VERSION=1.03

#
# Mounted volume to be monitored.
#
MOUNT="$1"

#
# Base path for all media directories
#
BASE_PATH="/home/plex/local-sorted"

#
# Search directories - one per line for easy maintenance
# Format: Relative path from BASE_PATH
#
SEARCH_DIRS=(
    # Movies
    "Movies"
    "3D Movies"
    
    # Special categories
    "Selfcare"
    "Workout"
    
    # TV Shows - Alphabetical for easy lookup
    "TV/1883"
    "TV/Ali G Rezurection"
    "TV/Andor"
    "TV/Band of Brothers"
    "TV/Better Call Saul"
    "TV/Boardwalk Empire"
    "TV/Business Doing Pleasure"
    "TV/Carnivàle"
    "TV/Chappelle's Show"
    "TV/Curb Your Enthusiasm"
    "TV/David Attenborough's Conquest of the Skies"
    "TV/David Attenborough's Kingdom of Plants"
    "TV/David Attenborough's Kingdom of Plants 3D"
    "TV/Fargo"
    "TV/Fireman Sam"
    "TV/Freaks and Geeks"
    "TV/Friends"
    "TV/Game of Thrones"
    "TV/Gangs of London"
    "TV/High Potential"
    "TV/Homeland"
    "TV/Horace and Pete"
    "TV/House of the Dragon"
    "TV/Law & Order"
    "TV/LEGO Star Wars"
    "TV/Looney Tunes"
    "TV/Mayor of Kingstown"
    "TV/Mister Rogers' Neighborhood"
    "TV/MythBusters"
    "TV/Our Planet"
    "TV/Penn & Teller- Bullshit!"
    "TV/Phineas and Ferb"
    "TV/Planet Earth II"
    "TV/Rick and Morty"
    "TV/Rome"
    "TV/Schitt's Creek"
    "TV/Seinfeld"
    "TV/Sesame Street"
    "TV/Severance"
    "TV/South Park"
    "TV/Station Eleven"
    "TV/The Americans (2013)"
    "TV/The Book of Boba Fett"
    "TV/The Expanse"
    "TV/The Golden Girls"
    "TV/The Hunt (2015)"
    "TV/The Leftovers"
    "TV/The Lone Gunmen"
    "TV/The Pacific"
    "TV/The Penguin"
    "TV/The Sopranos"
    "TV/The Vietnam War (2017)"
    "TV/The Wire"
    "TV/The X-Files"
    "TV/Top Gear"
    "TV/True Detective"
    "TV/Twin Peaks"
    "TV/Vikings"
    "TV/Wallace & Gromit's Cracking Contraptions"
    "TV/Wallace & Gromit's World of Invention"
    "TV/War"
    "TV/Westworld"
    "TV/Who Is America!"
)

# Build full paths from relative paths
declare -a SEARCH
for dir in "${SEARCH_DIRS[@]}"; do
    SEARCH+=("${BASE_PATH}/${dir}/")
done

#
# Maximum threshold of volume used as an integer that represents a percentage:
# 95 = 95%.
#
MAX_USAGE="$2"

#
# Failsafe mechanism. Delete a maximum of MAX_CYCLES files, raise an error after
# that. Prevents possible runaway script. Disable by choosing a high value.
#
MAX_CYCLES=10

#
# Log file location
#
LOG_FILE="/home/plex/dellocal.log"


show_header () {
    echo
    echo "DELETE OLD FILES $VERSION"
    echo
}

show_header

reset () {
    CYCLES=0
    OLDEST_FILE=""
    OLDEST_DATE=0
    ARCH=$(uname)
}

reset

if [ -z "$MOUNT" ] || [ ! -e "$MOUNT" ] || [ ! -d "$MOUNT" ] || [ -z "$MAX_USAGE" ]
then
    echo "Usage: $0 <mountpoint> <threshold>"
    echo "Where threshold is a percentage."
    echo
    echo "Example: $0 /storage 90"
    echo "If disk usage of /storage exceeds 90% the oldest"
    echo "file(s) will be deleted until usage is below 90%."
    echo
    echo "Wrong command line arguments or another error:"
    echo
    echo "- Directory not provided as argument or"
    echo "- Directory does not exist or"
    echo "- Argument is not a directory or"
    echo "- no/wrong percentage supplied as argument."
    echo
    exit 1
fi

check_capacity () {
    USAGE=$(df -h | grep "$MOUNT" | awk '{ print $5 }' | sed s/%//g)
    if [ ! "$?" == "0" ]
    then
        echo "Error: mountpoint $MOUNT not found in df output."
        exit 1
    fi

    if [ -z "$USAGE" ]
    then
        echo "Didn't get usage information of $MOUNT"
        echo "Mountpoint does not exist or please remove trailing slash."
        exit 1
    fi

    if [ "$USAGE" -gt "$MAX_USAGE" ]
    then
        echo "Usage of $USAGE% exceeded limit of $MAX_USAGE percent."
        return 0
    else
        echo "Usage of $USAGE% is within limit of $MAX_USAGE percent."
        return 1
    fi
}

check_age () {
    FILE="$1"
    if [ "$ARCH" == "Linux" ]
    then
        FILE_DATE=$(stat -c %Z "$FILE")
    elif [ "$ARCH" == "Darwin" ]
    then
        FILE_DATE=$(stat -f %Sm -t %s "$FILE")
    else
        echo "Error: unsupported architecture."
        echo "Send a patch for the correct stat arguments for your architecture."
    fi

    NOW=$(date +%s)
    AGE=$((NOW-FILE_DATE))
    if [ "$AGE" -gt "$OLDEST_DATE" ]
    then
        export OLDEST_DATE="$AGE"
        export OLDEST_FILE="$FILE"
    fi
}

process_file () {
    FILE="$1"

    #
    # Replace the following commands with whatever you want to do with
    # this file. You can delete files but also move files or do something else.
    #
    echo "Deleting oldest file $FILE"
    echo "[$(date +"%Y%m%d%H%M")] $FILE" >> "$LOG_FILE"
    rm "$FILE"
}

while check_capacity
do
    if [ "$CYCLES" -gt "$MAX_CYCLES" ]
    then
        echo "Error: after $MAX_CYCLES deleted files still not enough free space."
        exit 1
    fi

    reset

    FILES=$(find "${SEARCH[@]}" -type f -not -path '*/\.*')

    IFS=$'\n'
    for x in $FILES
    do
        check_age "$x"
    done

    if [ -e "$OLDEST_FILE" ]
    then
        #
        # Do something with file.
        #
        process_file "$OLDEST_FILE"
    else
        echo "Error: somehow, item $OLDEST_FILE disappeared."
    fi
    ((CYCLES++))
done
echo
