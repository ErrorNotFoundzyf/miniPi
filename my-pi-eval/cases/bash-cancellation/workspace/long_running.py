from pathlib import Path
from time import sleep


Path("ready.txt").touch()
sleep(8)
Path("finished.txt").touch()
