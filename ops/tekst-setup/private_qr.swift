import Foundation
import AppKit
import CoreImage
import Vision

// Existing owner credential arrives only on stdin, never in arguments/logs.
let payload = FileHandle.standardInput.readDataToEndOfFile()
guard payload.count < 4096,
      let text = String(data: payload, encoding: .utf8),
      text.hasPrefix("vless://"),
      let uri = URLComponents(string: text),
      uri.host == "64.188.80.39", uri.password == nil, uri.path.isEmpty,
      let port = uri.port,
      text.contains("@64.188.80.39:\(port)?") else {
    exit(2)
}
let filename: String
switch port {
case 2443: filename = "tcp-clean-test.png"
case 2444: filename = "tcp-tls-test.png"
default: exit(2)
}
let root = "/private/tmp/wayspn-tekst-auth-20261106"
let attributes = try FileManager.default.attributesOfItem(atPath: root)
guard (attributes[.posixPermissions] as? NSNumber)?.intValue == 0o700 else { exit(3) }
guard let filter = CIFilter(name: "CIQRCodeGenerator") else { exit(4) }
filter.setValue(payload, forKey: "inputMessage")
filter.setValue("M", forKey: "inputCorrectionLevel")
guard let raw = filter.outputImage else { exit(5) }
let scaled = raw.transformed(by: CGAffineTransform(scaleX: 8, y: 8))
let extent = scaled.extent.insetBy(dx: -32, dy: -32)
let background = CIImage(color: CIColor.white).cropped(to: extent)
let image = scaled.composited(over: background)
let context = CIContext(options: [.useSoftwareRenderer: true])
guard let cg = context.createCGImage(image, from: extent) else { exit(6) }
let request = VNDetectBarcodesRequest()
request.symbologies = [.qr]
try VNImageRequestHandler(cgImage: cg).perform([request])
guard request.results?.contains(where: { $0.payloadStringValue == text }) == true else { exit(7) }
let rep = NSBitmapImageRep(cgImage: cg)
guard let png = rep.representation(using: .png, properties: [:]) else { exit(8) }
let output = root + "/" + filename
try png.write(to: URL(fileURLWithPath: output), options: [.atomic])
try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: output)
print("Private QR generated and locally verified; credential not printed")
