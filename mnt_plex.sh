#!/bin/bash
export ENCFS6_CONFIG='/home/plex/encfs6.xml'
echo "Enter encryption passphrase"
read -s PASSPHRASE

#sudo systemctl start plexdrive.service
sudo mount -t nfs4 -o sec=krb5p,clientaddr=173.251.23.199 totalmonkey.cpam.tv:/home/spuniun /mnt/nfs/totalmonkey/

echo $PASSPHRASE | encfs -S /home/plex/.local-sorted /home/plex/local-sorted -o allow_other -o umask='002'
if [ $? -ne 0 ]; then
    echo "Error: Failed to mount local-sorted. Check your passphrase."
    exit 1
fi

echo $PASSPHRASE | encfs -S /home/plex/.gdrive-sorted /home/plex/gdrive-sorted -o allow_other
if [ $? -ne 0 ]; then
    echo "Error: Failed to mount gdrive-sorted. Check your passphrase."
    exit 1
fi

unionfs -o cow -o allow_other /home/plex/local-sorted=RW:/home/plex/gdrive-sorted=RO /home/plex/sorted

docker compose -f /home/plex/cpam-arrs/docker-compose.yml up -d
sleep 30
sudo systemctl start plexmediaserver.service
#cd /opt/telegram
#sudo -u spuniun screen -dm python start.py
cd /opt/Mellow
screen -dm npm start
cd /opt/MusicBot
screen -dm nodejs index.js
#cd /opt/PlexRecs
#sudo -u spuniun screen -dm python PlexRecsGeneral.py
