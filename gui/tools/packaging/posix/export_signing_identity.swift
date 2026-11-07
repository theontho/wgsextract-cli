import CryptoKit
import Foundation
import Security

func checked(_ status: OSStatus, _ operation: String) throws {
  guard status == errSecSuccess else {
    let message = SecCopyErrorMessageString(status, nil) as String? ?? "OSStatus \(status)"
    throw NSError(
      domain: "SigningExport", code: Int(status),
      userInfo: [NSLocalizedDescriptionKey: "\(operation): \(message)"]
    )
  }
}

func exportIdentity() throws {
  let arguments = CommandLine.arguments
  guard arguments.count == 3 || arguments.count == 4 else {
    throw NSError(
      domain: "SigningExport", code: 64,
      userInfo: [
        NSLocalizedDescriptionKey:
          "Usage: export_signing_identity.swift SHA1 OUTPUT_P12 [KEYCHAIN]; password on stdin"
      ]
    )
  }
  let fingerprint = arguments[1].uppercased()
  let passwordData = FileHandle.standardInput.readDataToEndOfFile()
  guard let password = String(data: passwordData, encoding: .utf8), !password.isEmpty else {
    throw NSError(
      domain: "SigningExport", code: 64,
      userInfo: [NSLocalizedDescriptionKey: "An export password is required on stdin."]
    )
  }
  var query: [CFString: Any] = [
    kSecClass: kSecClassIdentity,
    kSecReturnRef: true,
    kSecMatchLimit: kSecMatchLimitAll,
  ]
  if arguments.count == 4 {
    var keychain: SecKeychain?
    try checked(SecKeychainOpen(arguments[3], &keychain), "Open source keychain")
    guard let keychain else {
      throw NSError(
        domain: "SigningExport", code: 1,
        userInfo: [NSLocalizedDescriptionKey: "Opening the source keychain returned no keychain."]
      )
    }
    query[kSecMatchSearchList] = [keychain]
  }
  var result: CFTypeRef?
  try checked(SecItemCopyMatching(query as CFDictionary, &result), "Find signing identities")
  guard let identities = result as? [SecIdentity] else {
    throw NSError(
      domain: "SigningExport", code: 1,
      userInfo: [NSLocalizedDescriptionKey: "No signing identities were returned."]
    )
  }
  for identity in identities {
    var certificate: SecCertificate?
    try checked(SecIdentityCopyCertificate(identity, &certificate), "Read identity certificate")
    guard let certificate else {
      throw NSError(
        domain: "SigningExport", code: 1,
        userInfo: [NSLocalizedDescriptionKey: "Signing identity returned no certificate."]
      )
    }
    let data = SecCertificateCopyData(certificate) as Data
    let digest = Insecure.SHA1.hash(data: data).map { String(format: "%02X", $0) }.joined()
    guard digest == fingerprint else { continue }

    let passphrase = password as CFString
    var parameters = SecItemImportExportKeyParameters()
    parameters.version = UInt32(SEC_KEY_IMPORT_EXPORT_PARAMS_VERSION)
    parameters.passphrase = Unmanaged.passUnretained(passphrase)
    var exported: CFData?
    try checked(
      SecItemExport(identity, .formatPKCS12, [], &parameters, &exported),
      "Export selected signing identity"
    )
    guard let exported else {
      throw NSError(
        domain: "SigningExport", code: 1,
        userInfo: [NSLocalizedDescriptionKey: "Export returned no PKCS12 data."]
      )
    }
    try (exported as Data).write(to: URL(fileURLWithPath: arguments[2]))
    return
  }
  throw NSError(
    domain: "SigningExport", code: 1,
    userInfo: [NSLocalizedDescriptionKey: "No identity matches certificate SHA1 \(fingerprint)."]
  )
}

do {
  try exportIdentity()
} catch {
  FileHandle.standardError.write(Data("\(error.localizedDescription)\n".utf8))
  exit(1)
}
