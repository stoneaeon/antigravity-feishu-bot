import Cocoa
import Vision

func performOCR(on url: URL) {
    guard let image = NSImage(contentsOf: url),
          let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        print("ERROR_IMAGE")
        exit(1)
    }

    let request = VNRecognizeTextRequest { request, error in
        guard let observations = request.results as? [VNRecognizedTextObservation] else {
            print("ERROR_OCR")
            return
        }
        let recognizedText = observations.compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
        print(recognizedText)
    }

    request.recognitionLevel = .accurate
    if #available(macOS 11.0, *) {
        request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    }
    
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
        try handler.perform([request])
    } catch {
        print("ERROR_PERFORM")
    }
}

let args = CommandLine.arguments
if args.count > 1 {
    performOCR(on: URL(fileURLWithPath: args[1]))
} else {
    print("NO_FILE")
}
