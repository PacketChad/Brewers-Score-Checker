#!/usr/bin/python

# Brewers Pre-Game Checker
# v0.1.1
# This script can be scheduled in a CRON job to run daily.
# This script pulls the json data from the MLB site for the current day and determines game time, and if they are home or away.
# It then schedules a CRON job to start the score check script at game start time.

import requests
import datetime
import os
import re
from crontab import CronTab

#Remove the previous day's file
try:
    os.remove("/home/chad/brewers.txt")
    os.remove("/home/chad/brewers.log")
except OSError:
    pass

# Gather the current month, day, and year to be used in the URL
year = datetime.datetime.today().strftime('%Y')
month = datetime.datetime.today().strftime('%m')
day = datetime.datetime.today().strftime('%d')
formatted_date = month + "-" + day + "-" + year
url_date = year + "-" + month +  "-" + day

# Pull all games for the day
base_url = "http://statsapi.mlb.com/api/v1/schedule/games/?sportId=1&startDate=" + url_date + "&endDate=" + url_date

response = requests.get(base_url)
page_json = response.json()
page_str = str(response.content)

if 'Brewers' not in page_str:
    day_off = "The Brewers have the day off!"
    filename = "/home/chad/brewers.txt"
    fh = open("/home/chad/brewers.txt", "w")
    fh.write(formatted_date + '\n' + day_off)
    fh.close()

else:
    # Determine if Brewers are home or away & save the JSON for their upcoming game
    game_no = -1
    for game in page_json["dates"][0]["games"]:
        game_no += 1
        if game["teams"]["home"]["team"]["name"] == 'Milwaukee Brewers':
            #print("Brewers are home")
            home_or_away = 'home'
            gamePk = game["gamePk"]
            game_details = game
            brewers_game_index = game_no
        elif game["teams"]["away"]["team"]["name"] == 'Milwaukee Brewers':
            #print("Brewers are away")
            home_or_away = 'away'
            gamePk = game["gamePk"]
            game_details = game
            brewers_game_index = game_no
        else:
            #print("Brewers are not in gamePk:" + str(game["gamePk"]))
            pass

    # Determine start time
    z_start_time = re.search("[0-9][0-9]\:[0-9][0-9]", game_details["gameDate"])
    z_start_hour = z_start_time[0].split(':')[0]
    start_min = z_start_time[0].split(':')[1]
    if int(z_start_hour) < 5:
        c_start_hour = int(z_start_hour) + 19
    else:
        c_start_hour = int(z_start_hour) - 5
    
    # Write the file with all of the details
    filename = "/home/chad/brewers.txt"
    fh = open("/home/chad/brewers.txt", "w")
    fh.write(formatted_date + "\n" + base_url + "\n" + str(brewers_game_index) + "\n" + str(gamePk) + "\n" + home_or_away + "\n" + str(z_start_time[0]) + " ZULU" + "\n" + str(c_start_hour) + ":" + str(start_min) + " CDT")
    fh.close()

    # Create the CRON job to start checking the when the game starts
    cron = CronTab(user='chad')
    job = cron.new(command='python /home/chad/score_check.py', comment='brewers')
    job.minute.on(start_min)
    job.hour.on(c_start_hour)
    cron.write()
