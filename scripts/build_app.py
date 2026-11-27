"""Build standalone macOS native app bundle (`dist/bili2am.app`)."""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWIFT_SRC_DIR = ROOT / "macos" / "Bili2amApp"
DIST_APP = ROOT / "dist" / "bili2am.app"

ICON_GENERATOR_SWIFT = r"""
import AppKit
import CoreGraphics
import Foundation

let args = CommandLine.arguments
guard args.count >= 3, let sizeInt = Int(args[2]) else { exit(1) }
let outPath = args[1]
let size = CGFloat(sizeInt)

let rep = NSBitmapImageRep(
    bitmapDataPlanes: nil,
    pixelsWide: sizeInt,
    pixelsHigh: sizeInt,
    bitsPerSample: 8,
    samplesPerPixel: 4,
    hasAlpha: true,
    isPlanar: false,
    colorSpaceName: .deviceRGB,
    bytesPerRow: 0,
    bitsPerPixel: 0
)!

NSGraphicsContext.saveGraphicsState()
let ctx = NSGraphicsContext(bitmapImageRep: rep)!
NSGraphicsContext.current = ctx
let cg = ctx.cgContext

let rect = CGRect(x: size * 0.08, y: size * 0.08, width: size * 0.84, height: size * 0.84)
let path = CGPath(roundedRect: rect, cornerWidth: size * 0.20, cornerHeight: size * 0.20, transform: nil)
cg.addPath(path)
cg.clip()

let colors = [
    CGColor(srgbRed: 0.98, green: 0.14, blue: 0.27, alpha: 1.0),
    CGColor(srgbRed: 0.99, green: 0.36, blue: 0.44, alpha: 1.0),
] as CFArray
let grad = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: colors, locations: [0.0, 1.0])!
cg.drawLinearGradient(grad, start: CGPoint(x: 0, y: 0), end: CGPoint(x: size, y: size), options: [])

if let sym = NSImage(systemSymbolName: "music.note", accessibilityDescription: nil) {
    let cfg = NSImage.SymbolConfiguration(pointSize: size * 0.44, weight: .semibold)
    if let configured = sym.withSymbolConfiguration(cfg) {
        let tinted = configured.copy() as! NSImage
        tinted.lockFocus()
        NSColor.white.set()
        NSRect(origin: .zero, size: tinted.size).fill(using: .sourceAtop)
        tinted.unlockFocus()
        let drawRect = NSRect(
            x: (size - tinted.size.width) / 2,
            y: (size - tinted.size.height) / 2,
            width: tinted.size.width,
            height: tinted.size.height
        )
        tinted.draw(in: drawRect)
    }
}

NSGraphicsContext.restoreGraphicsState()
if let png = rep.representation(using: .png, properties: [:]) {
    try? png.write(to: URL(fileURLWithPath: outPath))
}
"""


def _generate_icns(out_icns: Path) -> bool:
    iconutil = shutil.which("iconutil")
    swift = shutil.which("swift")
    if not iconutil or not swift:
        return False

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        gen_swift = tmp / "gen_icon.swift"
        gen_swift.write_text(ICON_GENERATOR_SWIFT, encoding="utf-8")
        iconset = tmp / "AppIcon.iconset"
        iconset.mkdir()

        sizes = [
            ("icon_16x16.png", 16),
            ("icon_16x16@2x.png", 32),
            ("icon_32x32.png", 32),
            ("icon_32x32@2x.png", 64),
            ("icon_128x128.png", 128),
            ("icon_128x128@2x.png", 256),
            ("icon_256x256.png", 256),
            ("icon_256x256@2x.png", 512),
            ("icon_512x512.png", 512),
            ("icon_512x512@2x.png", 1024),
        ]
        for fname, px in sizes:
            proc = subprocess.run(
                [swift, str(gen_swift), str(iconset / fname), str(px)],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                return False

        proc2 = subprocess.run(
            [iconutil, "-c", "icns", str(iconset), "-o", str(out_icns)],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc2.returncode == 0 and out_icns.exists()


def build_macos_app(output_app: Path = DIST_APP) -> Path:
    swiftc = shutil.which("swiftc")
    if not swiftc:
        raise RuntimeError("未找到 swiftc，请安装 Xcode Command Line Tools。")

    swift_files = sorted(SWIFT_SRC_DIR.glob("*.swift"))
    if not swift_files:
        raise RuntimeError(f"未找到 Swift 源码目录: {SWIFT_SRC_DIR}")

    if output_app.exists():
        shutil.rmtree(output_app)

    macos_dir = output_app / "Contents" / "MacOS"
    res_dir = output_app / "Contents" / "Resources"
    macos_dir.mkdir(parents=True, exist_ok=True)
    res_dir.mkdir(parents=True, exist_ok=True)

    bin_path = macos_dir / "bili2am"
    cmd = [
        swiftc,
        "-O",
        "-parse-as-library",
        *[str(f) for f in swift_files],
        "-o",
        str(bin_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"编译 macOS App 失败:\n{proc.stderr}")

    icns_ok = _generate_icns(res_dir / "AppIcon.icns")
    plist_data = {
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleExecutable": "bili2am",
        "CFBundleIdentifier": "io.github.bili2am.app",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "bili2am",
        "CFBundleDisplayName": "bili2am",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    }
    if icns_ok:
        plist_data["CFBundleIconFile"] = "AppIcon"

    with (output_app / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump(plist_data, f)

    # Record current Python interpreter path so Finder-launched .app finds the installed package
    cfg_dir = Path.home() / ".config" / "bili2am"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "python_path").write_text(sys.executable, encoding="utf-8")

    return output_app


if __name__ == "__main__":
    app = build_macos_app()
    print(f"Built standalone macOS app: {app}")
