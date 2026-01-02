## Old Time Radio Wav #
A fork of zionbrock Radio player using the DFMini Player and a tiny2040.  This version will have these features:

 * 1 weeks worth of radio play
 * format of songs of the time, news, and commercials
 * unit will check time on startup
 * determine where in the playlist the time falls
 * start the song/newscast/commercial set for that time
 * continue on with the playlist for as long as the unit is on

 ### Issues as I see them

 1. DFMini player doesn't use playlists persay.  
 
    - The list is hard baked into the file system.
 2. Generating the playlist

    - there is a github which has a playlist script converting m3u into heirarchal folders with the proper songs/newscasts/commercials set 
3. Procuring enough songs/newscast/commercials to cover an entire week.
4. No Amplifier is required.  Will output up to 3W
5. Processes WAV, MP3, WMA Formats
6. How big of a drive will that take?
7. How big of a drive can the DFMini Player handle?
   - Limited to 32GB