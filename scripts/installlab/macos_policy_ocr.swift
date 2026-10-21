import Foundation
import ImageIO
import Vision

struct Label: Encodable {
    let text: String
    let x: Double
    let y: Double
}

guard CommandLine.arguments.count == 2 else {
    throw NSError(domain: "installlab.ocr", code: 1, userInfo: [
        NSLocalizedDescriptionKey: "Usage: macos_policy_ocr.swift IMAGE"
    ])
}
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    throw NSError(domain: "installlab.ocr", code: 2, userInfo: [
        NSLocalizedDescriptionKey: "Cannot read VM screenshot"
    ])
}
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["en-US"]
request.usesLanguageCorrection = false
try VNImageRequestHandler(cgImage: image).perform([request])
let labels = (request.results ?? []).compactMap { observation -> Label? in
    guard let candidate = observation.topCandidates(1).first else { return nil }
    let box = observation.boundingBox
    return Label(
        text: candidate.string,
        x: box.midX * Double(image.width),
        y: (1 - box.midY) * Double(image.height)
    )
}
FileHandle.standardOutput.write(try JSONEncoder().encode(labels))
