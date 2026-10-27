import Dispatch
import Foundation

func fail(_ message: String, code: Int32 = 78) -> Never {
    let data = Data(("Xiaoba AgentOS Runner: \(message)\n").utf8)
    FileHandle.standardError.write(data)
    exit(code)
}

let environment = ProcessInfo.processInfo.environment
guard let projectDirectory = environment["XIAOBA_PROJECT_DIR"], !projectDirectory.isEmpty else {
    fail("缺少 XIAOBA_PROJECT_DIR")
}
guard let configPath = environment["XIAOBA_CONFIG"], !configPath.isEmpty else {
    fail("缺少 XIAOBA_CONFIG")
}

let workerPath = URL(fileURLWithPath: projectDirectory).appendingPathComponent("bin/agentos").path
guard FileManager.default.isExecutableFile(atPath: workerPath) else {
    fail("Worker 不可执行：\(workerPath)")
}
guard FileManager.default.fileExists(atPath: configPath) else {
    fail("配置文件不存在：\(configPath)")
}

if environment["XIAOBA_RUNNER_CHECK_ONLY"] == "1" {
    print("{\"ok\":true,\"runner\":\"com.xiaoba.agentos.runner\"}")
    exit(0)
}

let child = Process()
child.executableURL = URL(fileURLWithPath: "/bin/zsh")
child.arguments = [workerPath, "--config", configPath, "run"]
child.currentDirectoryURL = URL(fileURLWithPath: projectDirectory)
child.standardOutput = FileHandle.standardOutput
child.standardError = FileHandle.standardError

var signalSources: [DispatchSourceSignal] = []
for signalNumber in [SIGTERM, SIGINT, SIGHUP] {
    signal(signalNumber, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: signalNumber, queue: .global())
    source.setEventHandler {
        if child.isRunning {
            child.terminate()
        }
    }
    source.resume()
    signalSources.append(source)
}

do {
    try child.run()
} catch {
    fail("无法启动 Worker：\(error)", code: 70)
}

child.waitUntilExit()
for source in signalSources {
    source.cancel()
}
exit(child.terminationStatus)
