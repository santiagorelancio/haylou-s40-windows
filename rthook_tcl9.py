# Tcl/Tk 9 trae su biblioteca embebida en la DLL; el hook de PyInstaller fija
# TCL_LIBRARY/TK_LIBRARY a carpetas ficticias, así que las quitamos.
import os
os.environ.pop("TCL_LIBRARY", None)
os.environ.pop("TK_LIBRARY", None)
