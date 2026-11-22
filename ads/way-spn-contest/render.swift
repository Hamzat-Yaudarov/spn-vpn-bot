import AppKit
import CoreText
import Foundation
import ImageIO
import UniformTypeIdentifiers

struct Rule: Decodable {
    let title: String
    let detail: [String]
}

struct Prize: Decodable {
    let place: String
    let prize: String
    let cash: Bool
}

struct Variant: Decodable {
    let id: String
    let palette: String
    let intro: String
    let headline: String
    let headlineUnit: String
    let tagline: String
    let coverLabel: String
}

struct Campaign: Decodable {
    let brand: String
    let campaign: String
    let bot: String
    let join: String
    let joinDescription: [String]
    let rules: [Rule]
    let prizes: [Prize]
    let prizeNote: [String]
    let drawNote: [String]
    let closeDate: String?
    let variants: [Variant]
}

let width = 1080
let height = 1920
let fps: Int32 = 30
let sceneLengths = [3.8, 5.4, 5.6, 4.4]
let duration = sceneLengths.reduce(0, +)
let sampleRate: Int32 = 44100
let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!
let bitmapInfo = CGBitmapInfo.byteOrder32Little.rawValue | CGImageAlphaInfo.premultipliedFirst.rawValue

func color(_ rgb: UInt32, _ alpha: CGFloat = 1) -> CGColor {
    CGColor(
        red: CGFloat((rgb >> 16) & 255) / 255,
        green: CGFloat((rgb >> 8) & 255) / 255,
        blue: CGFloat(rgb & 255) / 255,
        alpha: alpha
    )
}

struct Palette {
    let base: UInt32
    let accent: UInt32
    let secondary: UInt32
    let glow: UInt32
    let muted: UInt32

    init(_ name: String) {
        if name == "mint" {
            (base, accent, secondary, glow, muted) = (0x071913, 0xC4F7BE, 0x8DD6BB, 0x156B51, 0xA6BFB2)
        } else {
            (base, accent, secondary, glow, muted) = (0x101319, 0xFFDA82, 0xAED4F5, 0x244F80, 0xAEBAC8)
        }
    }
}

func clamp(_ value: Double) -> Double { min(1, max(0, value)) }

func entrance(_ time: Double, after delay: Double = 0) -> Double {
    1 - pow(1 - clamp((time - delay) / 0.45), 3)
}

struct Canvas {
    let ctx: CGContext

    func panel(_ rect: CGRect, radius: CGFloat, fill: CGColor, border: CGColor? = nil, stroke: CGFloat = 1) {
        let path = CGPath(roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil)
        ctx.setFillColor(fill)
        ctx.addPath(path)
        ctx.fillPath()
        if let border {
            ctx.setLineWidth(stroke)
            ctx.setStrokeColor(border)
            ctx.addPath(path)
            ctx.strokePath()
        }
    }

    func segment(_ start: CGPoint, _ end: CGPoint, color: CGColor, width: CGFloat = 1) {
        ctx.setStrokeColor(color)
        ctx.setLineWidth(width)
        ctx.setLineCap(.round)
        ctx.move(to: start)
        ctx.addLine(to: end)
        ctx.strokePath()
    }

    func circle(_ x: CGFloat, _ y: CGFloat, radius: CGFloat, tint: CGColor, solid: Bool = false) {
        let rect = CGRect(x: x - radius, y: y - radius, width: radius * 2, height: radius * 2)
        if solid {
            ctx.setFillColor(tint)
            ctx.fillEllipse(in: rect)
        } else {
            ctx.setStrokeColor(tint)
            ctx.setLineWidth(1.5)
            ctx.strokeEllipse(in: rect)
        }
    }

    func label(
        _ text: String, x: CGFloat, y: CGFloat, size: CGFloat, tint: CGColor,
        weight: NSFont.Weight = .semibold, spacing: CGFloat = 0, fit: CGFloat = .greatestFiniteMagnitude
    ) {
        func line(_ pointSize: CGFloat) -> CTLine {
            let font = NSFont.systemFont(ofSize: pointSize, weight: weight)
            return CTLineCreateWithAttributedString(NSAttributedString(string: text, attributes: [
                .font: font, .foregroundColor: tint, .kern: spacing,
            ]))
        }
        var glyphs = line(size)
        let measured = CTLineGetTypographicBounds(glyphs, nil, nil, nil)
        if measured > fit {
            glyphs = line(size * fit / measured)
        }
        ctx.saveGState()
        ctx.translateBy(x: x, y: y)
        ctx.scaleBy(x: 1, y: -1)
        ctx.textMatrix = .identity
        ctx.textPosition = .zero
        CTLineDraw(glyphs, ctx)
        ctx.restoreGState()
    }

    func reveal(_ t: Double, delay: Double = 0, offset: CGFloat = 42, _ body: () -> Void) {
        let p = entrance(t, after: delay)
        ctx.saveGState()
        ctx.setAlpha(p)
        ctx.translateBy(x: 0, y: (1 - p) * offset)
        body()
        ctx.restoreGState()
    }

    func glow(x: CGFloat, y: CGFloat, radius: CGFloat, tint: UInt32, alpha: CGFloat) {
        let gradient = CGGradient(
            colorsSpace: colorSpace, colors: [color(tint, alpha), color(tint, 0)] as CFArray,
            locations: [0, 1]
        )!
        let point = CGPoint(x: x, y: y)
        ctx.drawRadialGradient(gradient, startCenter: point, startRadius: 0,
                               endCenter: point, endRadius: radius, options: [])
    }
}

func background(_ c: Canvas, _ p: Palette) {
    c.ctx.setFillColor(color(p.base))
    c.ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
    c.glow(x: 900, y: 510, radius: 950, tint: p.glow, alpha: 0.63)
    c.glow(x: 220, y: 1640, radius: 690, tint: p.accent, alpha: 0.12)
    for x in stride(from: 42, to: width, by: 78) {
        c.segment(CGPoint(x: x, y: 0), CGPoint(x: x, y: height), color: color(0xFFFFFF, 0.040))
    }
    for y in stride(from: 24, to: height, by: 78) {
        c.segment(CGPoint(x: 0, y: y), CGPoint(x: width, y: y), color: color(0xFFFFFF, 0.040))
    }
}

func frameShell(_ c: Canvas, _ copy: Campaign, _ p: Palette, _ seconds: Double, _ scene: Int) {
    let white = color(0xF7FAFC)
    let accent = color(p.accent)
    c.panel(CGRect(x: 92, y: 190, width: 54, height: 54), radius: 14, fill: accent)
    c.label("S", x: 108, y: 230, size: 36, tint: color(p.base), weight: .black)
    c.label(copy.brand, x: 162, y: 228, size: 35, tint: white, weight: .heavy, spacing: 1.3, fit: 324)
    c.label(copy.campaign, x: 706, y: 227, size: 24, tint: color(p.muted), weight: .bold, spacing: 2.1, fit: 225)
    for index in sceneLengths.indices {
        let x = CGFloat(92 + index * 223)
        let start = sceneLengths.prefix(index).reduce(0, +)
        let part = clamp((seconds - start) / sceneLengths[index])
        c.panel(CGRect(x: x, y: 278, width: 201, height: 4), radius: 2, fill: color(0xFFFFFF, 0.15))
        if part > 0 {
            c.panel(CGRect(x: x, y: 278, width: 201 * part, height: 4), radius: 2, fill: accent)
        }
    }
    c.segment(CGPoint(x: 92, y: 1548), CGPoint(x: 962, y: 1548), color: color(0xFFFFFF, 0.21))
    c.label(copy.bot, x: 92, y: 1598, size: 25, tint: color(p.muted), weight: .bold, fit: 450)
    c.label("0\(scene + 1) / 0\(sceneLengths.count)", x: 843, y: 1598,
            size: 23, tint: accent, weight: .bold, spacing: 2)
}

func shield(_ c: Canvas, _ x: CGFloat, _ y: CGFloat, _ p: Palette, withFriend: Bool) {
    c.ctx.saveGState()
    c.ctx.translateBy(x: x, y: y)
    if withFriend {
        for d in [-72.0, 72.0] {
            c.circle(d, -42, radius: 38, tint: color(p.accent, 0.20), solid: true)
            c.circle(d, -42, radius: 38, tint: color(p.accent, 0.88))
            c.panel(CGRect(x: d - 51, y: 17, width: 102, height: 64), radius: 29,
                    fill: color(p.accent, 0.13), border: color(p.accent, 0.75), stroke: 2)
        }
        c.segment(CGPoint(x: -21, y: -42), CGPoint(x: 21, y: -42), color: color(p.accent, 0.85), width: 3)
    } else {
        let path = CGMutablePath()
        path.move(to: CGPoint(x: 0, y: -105))
        path.addQuadCurve(to: CGPoint(x: 91, y: -72), control: CGPoint(x: 48, y: -79))
        path.addLine(to: CGPoint(x: 91, y: -2))
        path.addQuadCurve(to: CGPoint(x: 0, y: 105), control: CGPoint(x: 89, y: 62))
        path.addQuadCurve(to: CGPoint(x: -91, y: -2), control: CGPoint(x: -89, y: 62))
        path.addLine(to: CGPoint(x: -91, y: -72))
        path.addQuadCurve(to: CGPoint(x: 0, y: -105), control: CGPoint(x: -48, y: -79))
        path.closeSubpath()
        c.ctx.addPath(path)
        c.ctx.setFillColor(color(p.accent, 0.16))
        c.ctx.setStrokeColor(color(p.accent))
        c.ctx.setLineWidth(4)
        c.ctx.drawPath(using: .fillStroke)
        c.label("S", x: -37, y: 45, size: 106, tint: color(p.accent), weight: .heavy)
    }
    c.ctx.restoreGState()
}

func hero(_ c: Canvas, _ variant: Variant, _ p: Palette, _ t: Double) {
    let withFriend = variant.palette == "mint"
    c.reveal(t, delay: 0.02) {
        c.label(variant.intro, x: 94, y: 434, size: 34, tint: color(p.secondary), weight: .bold, fit: 858)
        c.label(variant.headline, x: 83, y: 653, size: withFriend ? 174 : 238, tint: color(p.accent),
                weight: .black, spacing: -5, fit: 873)
        c.label(variant.headlineUnit, x: 94, y: 773, size: 114, tint: color(0xF7FAFC),
                weight: .heavy, spacing: -3, fit: 850)
        c.label(variant.tagline, x: 97, y: 857, size: 45, tint: color(p.muted), weight: .medium, fit: 848)
    }
    c.reveal(t, delay: 0.22, offset: 94) {
        let x: CGFloat = 527
        let y: CGFloat = 1195 + 12 * sin(t * 2.2)
        c.glow(x: x, y: y, radius: 338, tint: p.accent, alpha: 0.21)
        for index in 0..<4 {
            let r: CGFloat = 146 + CGFloat(index) * 37
            c.circle(x, y, radius: r, tint: color(p.secondary, index == 0 ? 0.48 : 0.22))
            let a = t * (index.isMultiple(of: 2) ? 1.05 : -0.65) + Double(index) * 1.45
            let dotX = x + cos(a) * r
            let dotY = y + sin(a) * r
            c.circle(dotX, dotY, radius: 8 - CGFloat(index), tint: color(p.accent, 0.19), solid: true)
            c.circle(dotX, dotY, radius: 4 - CGFloat(index) * 0.32, tint: color(p.accent), solid: true)
        }
        shield(c, x, y, p, withFriend: withFriend)
        if !variant.coverLabel.isEmpty {
            c.panel(CGRect(x: 122, y: 1026, width: withFriend ? 258 : 100, height: 54), radius: 27,
                    fill: color(p.accent), border: color(p.accent))
            c.label(variant.coverLabel, x: 144, y: 1062, size: 25, tint: color(p.base),
                    weight: .black, spacing: 1, fit: withFriend ? 214 : 56)
        }
    }
}

func prizes(_ c: Canvas, _ copy: Campaign, _ p: Palette, _ t: Double) {
    c.reveal(t) {
        c.label("10 призовых мест", x: 90, y: 476, size: 97, tint: color(0xF7FAFC),
                weight: .heavy, spacing: -3, fit: 872)
        c.label("МЕСТО", x: 126, y: 570, size: 24, tint: color(p.muted), weight: .bold, spacing: 2)
        c.label("ПРИЗ", x: 342, y: 570, size: 24, tint: color(p.muted), weight: .bold, spacing: 2)
    }
    for (index, prize) in copy.prizes.enumerated() {
        let y = CGFloat(598 + index * 107 + (prize.cash ? 0 : 23))
        c.reveal(t, delay: 0.10 + Double(index) * 0.10, offset: 28) {
            let accent = prize.cash ? p.accent : p.secondary
            c.panel(CGRect(x: 92, y: y, width: 872, height: 92), radius: 28,
                    fill: color(p.base, 0.83), border: color(accent, 0.42), stroke: 2)
            c.label(prize.place, x: 126, y: y + 62, size: 43, tint: color(accent), weight: .heavy, fit: 142)
            c.segment(CGPoint(x: 280, y: y + 20), CGPoint(x: 280, y: y + 72), color: color(accent, 0.29))
            c.label(prize.prize, x: 342, y: y + 63, size: 54, tint: color(0xF7FAFC), weight: .heavy, fit: 390)
            if !prize.cash {
                c.label("ПОДПИСКИ", x: 746, y: y + 60, size: 22, tint: color(p.muted), weight: .bold,
                        spacing: 1.1, fit: 182)
            }
        }
    }
}

func rules(_ c: Canvas, _ copy: Campaign, _ p: Palette, _ t: Double) {
    c.reveal(t) {
        c.label("Два условия", x: 89, y: 483, size: 106, tint: color(0xF7FAFC),
                weight: .heavy, spacing: -4, fit: 876)
        c.label("ДЛЯ ПОЛУЧЕНИЯ ПРИЗА", x: 96, y: 553, size: 28, tint: color(p.accent),
                weight: .heavy, spacing: 2.8, fit: 820)
    }
    for (index, rule) in copy.rules.enumerated() {
        let y = CGFloat(646 + index * 307)
        c.reveal(t, delay: 0.15 + Double(index) * 0.21) {
            c.panel(CGRect(x: 92, y: y, width: 872, height: 266), radius: 38,
                    fill: color(p.base, 0.88), border: color(p.secondary, 0.48), stroke: 2)
            c.panel(CGRect(x: 126, y: y + 35, width: 88, height: 63), radius: 21,
                    fill: color(index == 0 ? p.accent : p.secondary))
            c.label("0\(index + 1)", x: 148, y: y + 78, size: 32, tint: color(p.base), weight: .black)
            c.label(rule.title, x: 245, y: y + 87, size: 59, tint: color(0xF7FAFC),
                    weight: .heavy, spacing: -1.3, fit: 686)
            for (line, text) in rule.detail.enumerated() {
                c.label(text, x: 128, y: y + 163 + CGFloat(line) * 49, size: 39,
                        tint: color(p.muted), weight: .semibold, fit: 793)
            }
        }
    }
    c.reveal(t, delay: 0.58) {
        for (line, text) in copy.drawNote.enumerated() {
            c.label(text, x: 95, y: 1330 + CGFloat(line) * 43, size: 35, tint: color(p.muted),
                    weight: .semibold, fit: 856)
        }
        if let date = copy.closeDate {
            c.label(date, x: 95, y: 1454, size: 35, tint: color(p.accent), weight: .bold, fit: 856)
        }
    }
}

func join(_ c: Canvas, _ copy: Campaign, _ p: Palette, _ t: Double) {
    c.reveal(t) {
        c.label("Вступайте", x: 91, y: 494, size: 112, tint: color(0xF7FAFC),
                weight: .black, spacing: -3, fit: 863)
        c.label("через Telegram", x: 94, y: 604, size: 90, tint: color(p.accent),
                weight: .heavy, spacing: -3, fit: 860)
    }
    c.reveal(t, delay: 0.13) {
        c.panel(CGRect(x: 92, y: 703, width: 872, height: 339), radius: 40,
                fill: color(p.base, 0.85), border: color(p.secondary, 0.6), stroke: 2)
        c.label(copy.bot, x: 132, y: 795, size: 51, tint: color(0xF7FAFC), weight: .bold, fit: 785)
        for (line, text) in copy.joinDescription.enumerated() {
            c.label(text, x: 132, y: 864 + CGFloat(line) * 45, size: 35,
                    tint: color(p.muted), weight: .medium, fit: 790)
        }
        c.panel(CGRect(x: 128, y: 945, width: 800, height: 66), radius: 23, fill: color(p.accent))
        c.label(copy.join, x: 160, y: 990, size: 36, tint: color(p.base), weight: .heavy, fit: 660)
        let y = 978.0
        c.segment(CGPoint(x: 847, y: y), CGPoint(x: 890, y: y), color: color(p.base), width: 4)
        c.segment(CGPoint(x: 890, y: y), CGPoint(x: 875, y: y - 13), color: color(p.base), width: 4)
        c.segment(CGPoint(x: 890, y: y), CGPoint(x: 875, y: y + 13), color: color(p.base), width: 4)
    }
    c.reveal(t, delay: 0.32) {
        for (line, text) in copy.prizeNote.enumerated() {
            c.label(text, x: 94, y: 1184 + CGFloat(line) * 55, size: 43, tint: color(0xF7FAFC),
                    weight: .semibold, fit: 869)
        }
        c.label("ПОЛНЫЕ УСЛОВИЯ В БОТЕ", x: 97, y: 1390, size: 26, tint: color(p.accent),
                weight: .bold, spacing: 2.1, fit: 860)
    }
}

func film(_ ctx: CGContext, _ copy: Campaign, _ variant: Variant, _ seconds: Double, underlay: CGImage? = nil) {
    let p = Palette(variant.palette)
    let c = Canvas(ctx: ctx)
    var scene = 0
    var t = seconds
    while scene < sceneLengths.count - 1 && t >= sceneLengths[scene] {
        t -= sceneLengths[scene]
        scene += 1
    }
    ctx.saveGState()
    if let underlay {
        ctx.draw(underlay, in: CGRect(x: 0, y: 0, width: width, height: height))
    }
    ctx.translateBy(x: 0, y: CGFloat(height))
    ctx.scaleBy(x: 1, y: -1)
    if underlay == nil { background(c, p) }
    frameShell(c, copy, p, seconds, scene)
    switch scene {
    case 0: hero(c, variant, p, t)
    case 1: prizes(c, copy, p, t)
    case 2: rules(c, copy, p, t)
    default: join(c, copy, p, t)
    }
    ctx.restoreGState()
}

func snapshot(_ campaign: Campaign, _ variant: Variant, _ seconds: Double, _ url: URL) throws {
    let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                        bytesPerRow: 0, space: colorSpace, bitmapInfo: bitmapInfo)!
    film(ctx, campaign, variant, seconds)
    let image = NSBitmapImageRep(cgImage: ctx.makeImage()!)
    try image.representation(using: .jpeg, properties: [.compressionFactor: 0.92])!.write(to: url)
}

func stream(_ campaign: Campaign, _ variant: Variant) throws {
    let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                        bytesPerRow: 0, space: colorSpace, bitmapInfo: bitmapInfo)!
    let backdrop = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                             bytesPerRow: 0, space: colorSpace, bitmapInfo: bitmapInfo)!
    backdrop.translateBy(x: 0, y: CGFloat(height))
    backdrop.scaleBy(x: 1, y: -1)
    background(Canvas(ctx: backdrop), Palette(variant.palette))
    let underlay = backdrop.makeImage()!
    let settings = [kCGImageDestinationLossyCompressionQuality: 0.98] as CFDictionary
    for number in 0..<Int((duration * Double(fps)).rounded()) {
        film(ctx, campaign, variant, Double(number) / Double(fps), underlay: underlay)
        let frame = NSMutableData()
        let encoder = CGImageDestinationCreateWithData(frame, UTType.jpeg.identifier as CFString, 1, nil)!
        CGImageDestinationAddImage(encoder, ctx.makeImage()!, settings)
        guard CGImageDestinationFinalize(encoder) else {
            throw NSError(domain: "Could not encode frame \(number)", code: 5)
        }
        try FileHandle.standardOutput.write(contentsOf: frame as Data)
    }
}

func wav(_ url: URL) throws {
    let rate = Double(sampleRate)
    let count = Int(duration * rate)
    var data = Data()
    func int(_ value: Int) {
        var bits = UInt32(value).littleEndian
        withUnsafeBytes(of: &bits) { data.append(contentsOf: $0) }
    }
    func short(_ value: Int16) {
        var bits = value.littleEndian
        withUnsafeBytes(of: &bits) { data.append(contentsOf: $0) }
    }
    data.append(contentsOf: "RIFF".utf8)
    int(36 + count * 2)
    data.append(contentsOf: "WAVEfmt ".utf8)
    int(16)
    short(1)
    short(1)
    int(Int(sampleRate))
    int(Int(sampleRate * 2))
    short(2)
    short(16)
    data.append(contentsOf: "data".utf8)
    int(count * 2)

    let quarter = 0.5
    let bass = [55.0, 55.0, 65.406, 49.0]
    let notes = [220.0, 261.625, 329.628, 293.665, 220.0, 329.628, 392.0, 261.625]
    var noiseSeed: UInt32 = 17
    for i in 0..<count {
        let t = Double(i) / rate
        let downbeat = t.truncatingRemainder(dividingBy: quarter)
        let offbeat = (t + quarter / 2).truncatingRemainder(dividingBy: quarter)
        let arpTime = t.truncatingRemainder(dividingBy: quarter / 2)
        let key = bass[Int(t / (quarter * 4)) % bass.count]
        let note = notes[Int(t / (quarter / 2)) % notes.count]
        let kick = sin(2 * .pi * (49 * downbeat + 5 * (1 - exp(-26 * downbeat)))) * exp(-15 * downbeat)
        let envelope = min(1, downbeat * 95) * exp(-5.7 * downbeat)
        let low = (sin(2 * .pi * key * t) + 0.21 * sin(4 * .pi * key * t)) * envelope
        let pluck = (sin(2 * .pi * note * arpTime) + 0.2 * sin(4 * .pi * note * arpTime))
            * min(1, arpTime * 145) * exp(-15 * arpTime)
        noiseSeed = 1_664_525 &* noiseSeed &+ 1_013_904_223
        let noise = (Double(noiseSeed >> 16) / 32_767.5 - 1) * exp(-70 * offbeat)
        let fade = clamp(t * 6) * clamp((duration - t) * 2.5)
        let mix = fade * (0.31 * kick + 0.16 * low + 0.20 * pluck + 0.045 * noise)
        short(Int16((mix * 32767).rounded()))
    }
    try data.write(to: url)
}

let arguments = Array(CommandLine.arguments.dropFirst())
guard arguments.count >= 2 else {
    throw NSError(domain: "Usage: render content.json <frames|stills|audio> ...", code: 1)
}

let campaign = try JSONDecoder().decode(
    Campaign.self, from: Data(contentsOf: URL(fileURLWithPath: arguments[0]))
)
guard campaign.rules.count == 2 && campaign.rules.allSatisfy({ $0.detail.count <= 2 })
    && campaign.prizes.count == 7 && campaign.joinDescription.count <= 2 && campaign.prizeNote.count <= 2
    && campaign.drawNote.count <= 2 else {
    throw NSError(domain: "Expected two rules, seven prize rows and at most two lines per caption", code: 2)
}

let option = arguments.dropFirst(2).first ?? ""
switch arguments[1] {
case "list":
    campaign.variants.forEach { print($0.id) }
case "audio" where !option.isEmpty:
    try wav(URL(fileURLWithPath: option))
case "frames":
    guard let variant = campaign.variants.first(where: { $0.id == option }) else {
        throw NSError(domain: "Unknown variant: \(option)", code: 3)
    }
    try stream(campaign, variant)
case "stills" where !option.isEmpty:
    let directory = URL(fileURLWithPath: option, isDirectory: true)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    for variant in campaign.variants {
        for (index, time) in [1.25, 6.0, 11.4, 16.3].enumerated() {
            let image = directory.appendingPathComponent("\(variant.id)-\(index + 1).jpg")
            try snapshot(campaign, variant, time, image)
        }
    }
default:
    throw NSError(domain: "Unknown render command", code: 4)
}
