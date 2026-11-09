# Qt-Progressive-JXL
Simple experiment for a PySide6 viewer for JpegXL files with incremental loading and progressive JXL support using libjxl directly  
Is vibecoded

<img width="1330" height="926" alt="image" src="https://github.com/user-attachments/assets/3f50708a-6966-41b5-a925-e2af20581dec" />

Tested with Python 3.14, PySide6 6.12.0, libjxl 0.12.0, Arch Linux (CachyOS) 7.3.3

There's a weird issue with progressively encoded JXL files inbetween distance > 0 and distance < 0.51.  
JXL files encoded within this distance range will have their last, high-quality 'blocks' appear full red (probably bug on this side, idk i couldnt reproduce on my system Qt image viewer, also using libjxl 0.12.0 or using browser's jxl-rs)

- has a dropdown in-app for choosing jxl files, put files in same dir as the script
- forward decode will reuse decoder, going backwards with slider will destroy decoder and remake it
- rest is self explanatory probably
