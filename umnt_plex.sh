#!/bin/bash
sudo systemctl stop plexmediaserver.service

docker compose -f /home/plex/cpam-arrs/docker-compose.yml down

fusermount -u /home/plex/sorted/
sleep 15
fusermount -u /home/plex/gdrive-sorted
fusermount -u /home/plex/local-sorted

#sudo systemctl stop plexdrive.service
sudo umount /mnt/nfs/totalmonkey
