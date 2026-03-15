#!/user/bin/python

# Brewers Score Checker
# v0.1.1
# This script pulls the latest score throughout a game for the Brewers.
# If they score, it sends an HTTP POST request to flash the lights

import requests
import os
import time
from crontab import CronTab

try:
    # Opens the file made by the daily script, pulls out the URL, Home/Away, and game index.
    raw = open("brewers.txt", 'r')
    lines = raw.readlines()
    url = lines[1]
    url = url.rstrip('\n')
    hoa = lines[4]
    hoa = hoa.rstrip('\n')
    game_index = lines[2]
    game_index = game_index.rstrip('\n')
    raw.close()
    
    response = requests.get(url)
    page_json = response.json()
    game_json = page_json["dates"][0]["games"][int(game_index)]
    runs_after = 0
    i = 0
    while game_json["status"]["detailedState"] != "Final":
        # Pulls the text for runs scored.  If score doesn't exist yet, set it to 0.
        try:
            runs_before = game_json["teams"][hoa]["score"]
        except:
            runs_before = 0
        localtime = time.asctime(time.localtime(time.time()))
        i += 1
    
        # This is used to create a log that counts the number of times the page has been pulled, along with the run counters.
        raw_log = open("brewers.log", 'a')
        raw_log.write("=================" + "\n" + "Iteration: " + str(i) + "\n")
        raw_log.write(localtime + "\n")
        raw_log.write("Before Runs: " + str(runs_before) + "\n")
        raw_log.write("After Runs: " + str(runs_after) + "\n")
        raw_log.close()
        
        if int(runs_before) > int(runs_after):
            #HTTP POST
            b1 = requests.post('<URL to flash lights>')
            jb1 = 1
            while b1.status_code != 200:
                b1 = requests.post('<URL to flash lights>')
                jb1 += 1
                time.sleep(9)
            raw_log = open("brewers.log", 'a')
            raw_log.write(localtime + "\n")
            raw_log.write("Ran BEFORE HTTP POST." + "\n")
            raw_log.write("B1 ran " + str(jb1) + " time(s)." + "\n")
            raw_log.close()
        else:
            pass

        # Pauses and gets the data again.
        time.sleep(5)
        response = requests.get(url)
        page_json = response.json()
        game_json = page_json["dates"][0]["games"][int(game_index)]

        # Subsequent check for runs.  If score doesn't exist yet, set it to 0.
        try:
            runs_after = game_json["teams"][hoa]["score"]
        except:
            runs_after = 0
        localtime = time.asctime(time.localtime(time.time()))
        i += 1
        raw_log = open("brewers.log", 'a')
        raw_log.write("=================" + "\n" +
                      "Iteration: " + str(i) + "\n")
        raw_log.write(localtime + "\n")
        raw_log.write("Before Runs: " + str(runs_before) + "\n")
        raw_log.write("After Runs: " + str(runs_after) + "\n")
        raw_log.close()

        if int(runs_after) > int(runs_before):
            #HTTP POST
            a1 = requests.post('URL to flash lights')
            ja1 = 1
            while a1.status_code != 200:
                a1 = requests.post('URL to flash lights')
                ja1 += 1
                time.sleep(9)
            raw_log = open("brewers.log", 'a')
            raw_log.write(localtime + "\n")
            raw_log.write("Ran BEFORE HTTP POST." + "\n")
            raw_log.write("A1 ran " + str(ja1) + " time(s)." + "\n")
            raw_log.close()
        else:
            pass

        time.sleep(5)
        response = requests.get(url)
        page_json = response.json()
    
    # Resets lights to soft white and turns them off
    f1 = requests.post('URLto reset lights')
    
    # Removes CRONTAB entry after the game is over
    my_cron = CronTab(user='chad')
    my_cron.remove_all(comment='brewers')
    my_cron.write()

    # Writes final text to log file
    localtime = time.asctime(time.localtime(time.time()))
    raw_log = open("brewers.log", 'a')
    raw_log.write("=================" + "\n")
    raw_log.write(localtime + "\n")
    raw_log.write("GAME OVER" + "\n")
    raw_log.close()

except Exception as e:
    # If there's an exception, log the details and schedule a restart of the script.
    localtime = time.asctime(time.localtime(time.time()))
    excepname = type(e).__name__
    raw_log = open("brewers_error.log", 'a')
    raw_log.write(localtime + "\n")
    raw_log.write(excepname + "\n")
    raw_log.write(e.__doc__ + "\n")
    raw_log.write(e.message + "\n")
    raw_log.close()
    localtime = time.asctime(time.localtime(time.time()))
    restart_hour1 = localtime.split(':')[0]
    hr_length = len(restart_hour1)
    hr_length = hr_length - 2
    restart_hour2 = restart_hour1[11:]
    restart_hour = int(restart_hour2)
    restart_min1 = localtime.split(':')[1]
    restart_min2 = int(restart_min1) + 2
    if restart_min2 == 60:
        restart_min = 0
    elif restart_min2 == 61:
        restart_min = 1
    else:
        restart_min = restart_min2
    cron = CronTab(user='chad')
    job = cron.new(command='python /home/chad/score_check.py', comment='brewers')
    job.minute.on(restart_min)
    job.hour.on(restart_hour)
    cron.write()
