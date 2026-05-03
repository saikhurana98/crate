I want a high quality music file downloader. Here are the basic requirements i want from this music Downloader. 

1. Load Balanced Across Multiple sources  ( a healthy mix of websites and telegram bots)
2. Download straight to NAS ( allow smb/nfs mount ) and maintain an active sqlitedb of all the downloaded tracks
3. FLACs only 
4. Audio Verification post download if unsure. If perfect metadata match then there's no need. only under a threshold of uncertainty. 
5. Should be able to downlod from using spotify / apple music / youtube music link or just by the name of the music. offer multiple options if unsure and then verify

Music Sources
1. Squid WTF - Qobuz - https://qobuz.squid.wtf/
2. Lucidia - https://lucida.to/
3. Monochrome - https://monochrome.tf/
4. Soulseed
5. OnTheSpot - Ripping the track if nothing else works


Download Settings
1. File Format - FLAC
2. Bitrate - 24 Bit @ 192khz 
3. Reduce quality if the download speed is slow for the flac file or if the flac isn't available at all. If the download speed is low then eventually ensure the flac replaces the poor quality audio track. 

Load Balance Setup
1. Multi Threaded - Download from multiple sources at the same time. Fastest download wins. discard the rest

Verification and Metadata Tagging
1. Post download completion, TAG the music metadata properly 