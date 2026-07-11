"""
Run this once to create the Callback Manager desktop icon on Windows.
Usage:  python create_desktop_icon.py
"""
import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw

# Bright call-green - distinct from Endoscopy Manager's navy, and from the
# default generic icons everything else on the desktop uses.
GREEN = (0, 191, 99, 255)
WHITE = (255, 255, 255, 255)


def create_ico(ico_path):
    """Draw a rounded-square green badge with a white phone-handset glyph,
    rendered big then downsampled into the standard Windows icon sizes."""
    S = 256
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle([8, 8, S - 8, S - 8], radius=56, fill=GREEN)

    # Simple "handset" glyph: a rounded bar with a bigger round bell at each
    # end (a dumbbell), drawn upright then rotated into the classic
    # diagonal phone-receiver pose. Reads clearly even at 16-32px.
    glyph = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glyph)
    cx = S // 2
    bar_w = 40
    top, bottom = 66, S - 66
    gdraw.rounded_rectangle([cx - bar_w // 2, top, cx + bar_w // 2, bottom],
                             radius=bar_w // 2, fill=WHITE)
    bell_r = 34
    gdraw.ellipse([cx - bell_r, top - bell_r + 18, cx + bell_r, top + bell_r + 18], fill=WHITE)
    gdraw.ellipse([cx - bell_r, bottom - bell_r - 18, cx + bell_r, bottom + bell_r - 18], fill=WHITE)
    glyph = glyph.rotate(-45, resample=Image.BICUBIC, center=(cx, S // 2))

    img.alpha_composite(glyph)

    sizes = [16, 24, 32, 48, 64, 128, 256]
    img.save(ico_path, format='ICO', sizes=[(s, s) for s in sizes])


def get_windows_desktop():
    """Return the real Desktop path (handles OneDrive redirection)."""
    result = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         '[Environment]::GetFolderPath("Desktop")'],
        capture_output=True, text=True)
    path = result.stdout.strip()
    return path if path else os.path.join(os.path.expanduser('~'), 'Desktop')


def create_windows_shortcut(app_dir, ico_path):
    """Create 'Callback Manager.lnk' on the Windows Desktop via PowerShell."""
    target = os.path.join(app_dir, 'run_app.bat')

    ps = f"""
$desktop  = [Environment]::GetFolderPath('Desktop')
$shortcut = Join-Path $desktop 'Callback Manager.lnk'
$ws = New-Object -ComObject WScript.Shell
$s  = $ws.CreateShortcut($shortcut)
$s.TargetPath       = "{target}"
$s.WorkingDirectory = "{app_dir}"
$s.IconLocation     = "{ico_path},0"
$s.Description      = "Callback Manager - patient callback queue"
$s.WindowStyle      = 1
$s.Save()
Write-Output $shortcut
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.ps1',
                                     delete=False, encoding='utf-8') as f:
        f.write(ps)
        ps_file = f.name

    result = subprocess.run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ps_file],
        capture_output=True, text=True)
    os.unlink(ps_file)

    created = result.stdout.strip()
    return created if created and os.path.exists(created) else None


if __name__ == '__main__':
    app_dir = os.path.dirname(os.path.abspath(__file__))
    ico_path = os.path.join(app_dir, 'icon.ico')

    print()
    print('  Callback Manager - Desktop Icon Setup')
    print('  --------------------------------------')

    if sys.platform != 'win32':
        print('  This script is for Windows only.')
        print(f'  On other systems, create a shortcut to:  {app_dir}/run_app.bat')
        sys.exit(1)

    try:
        create_ico(ico_path)
        print(f'  OK Icon created ({ico_path})')
    except Exception as e:
        print(f'  !! Icon creation failed: {e}')
        ico_path = ''

    shortcut = create_windows_shortcut(app_dir, ico_path)
    if shortcut:
        print('  OK Shortcut created on Desktop')
        print()
        print('  ==========================================')
        print('  Double-click "Callback Manager" on your ')
        print('  Desktop to launch the app.')
        print('  ==========================================')
    else:
        print()
        print('  !! Could not create shortcut automatically.')
        print('    Right-click the Desktop -> New -> Shortcut')
        print(f'   Target:  {app_dir}\\run_app.bat')

    print()
    try:
        input('  Press Enter to close...')
    except (EOFError, OSError):
        pass
