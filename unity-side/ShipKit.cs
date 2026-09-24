using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using UnityEngine;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;

/// <summary>
/// Build entry points for ship_android.py and pipeline.py (Android AAB, iOS Xcode project).
///
/// Copied into the project just before the build — never commit it into a game repo.
/// The class name is deliberately unique (several projects already define their own
/// `BuildScript`, which would collide).
///
/// Invoked as:
///   Unity -batchmode -quit -projectPath &lt;repo&gt; -buildTarget Android|iOS
///         -executeMethod ShipKit.Android | ShipKit.IOS
///         -buildOutput &lt;out.aab | xcode dir&gt; -shipkitVersion &lt;x.y&gt; -shipkitCode &lt;n&gt;
///         [-targetSdk 36 -keystore &lt;path&gt; -keyalias &lt;alias&gt;]     (Android)
///         [-shipkitDev true] [-defineAdd FOO,BAR] [-defineRemove BAZ]
///         [-prebuildMethod Namespace.Class.Method]
///
/// Keystore passwords come from the environment (SHIPKIT_KEYSTORE_PASS /
/// SHIPKIT_KEYALIAS_PASS), never argv — argv is world-readable via `ps`.
/// </summary>
public static class ShipKit
{
    public static void Android()
    {
        string outputPath = GetArg("-buildOutput");
        string targetSdk  = GetArg("-targetSdk");
        string keystore   = GetArg("-keystore");
        string keyalias   = GetArg("-keyalias");
        if (string.IsNullOrEmpty(outputPath)) { Fail("-buildOutput is required"); return; }

        if (!ApplyCommon(NamedBuildTarget.Android)) return;

        string buildCode = GetArg("-shipkitCode");
        if (!string.IsNullOrEmpty(buildCode))
        {
            PlayerSettings.Android.bundleVersionCode = int.Parse(buildCode);
            Console.WriteLine($"[ShipKit] bundleVersionCode = {buildCode}");
        }

        if (!string.IsNullOrEmpty(targetSdk))
        {
            // Cast from int: the AndroidSdkVersions enum name lags new API levels.
            PlayerSettings.Android.targetSdkVersion = (AndroidSdkVersions)int.Parse(targetSdk);
            Console.WriteLine($"[ShipKit] targetSdkVersion = {PlayerSettings.Android.targetSdkVersion} ({targetSdk})");
        }

        if (!string.IsNullOrEmpty(keystore))
        {
            string storePass = Environment.GetEnvironmentVariable("SHIPKIT_KEYSTORE_PASS");
            string aliasPass = Environment.GetEnvironmentVariable("SHIPKIT_KEYALIAS_PASS");
            if (!File.Exists(keystore)) { Fail($"Keystore not found: {keystore}"); return; }
            if (string.IsNullOrEmpty(storePass) || string.IsNullOrEmpty(aliasPass))
            {
                Fail("SHIPKIT_KEYSTORE_PASS / SHIPKIT_KEYALIAS_PASS not set — refusing to " +
                     "produce a debug-signed build that Play will reject.");
                return;
            }
            PlayerSettings.Android.useCustomKeystore = true;
            PlayerSettings.Android.keystoreName = keystore;
            PlayerSettings.Android.keystorePass = storePass;
            PlayerSettings.Android.keyaliasName = keyalias;
            PlayerSettings.Android.keyaliasPass = aliasPass;
            Console.WriteLine($"[ShipKit] Signing with {Path.GetFileName(keystore)} alias '{keyalias}'");
        }

        EditorUserBuildSettings.buildAppBundle = true;
        Directory.CreateDirectory(Path.GetDirectoryName(outputPath));
        Build(outputPath, BuildTarget.Android, BuildTargetGroup.Android);
    }

    public static void IOS()
    {
        string outputPath = GetArg("-buildOutput");
        if (string.IsNullOrEmpty(outputPath)) { Fail("-buildOutput is required"); return; }

        if (!ApplyCommon(NamedBuildTarget.iOS)) return;

        string minIos = GetArg("-iosMinVersion");
        if (!string.IsNullOrEmpty(minIos))
        {
            PlayerSettings.iOS.targetOSVersionString = minIos;
            Console.WriteLine($"[ShipKit] iOS.targetOSVersion = {minIos}");
        }

        string buildCode = GetArg("-shipkitCode");
        if (!string.IsNullOrEmpty(buildCode))
        {
            PlayerSettings.iOS.buildNumber = buildCode;
            Console.WriteLine($"[ShipKit] iOS.buildNumber = {buildCode}");
        }
        Build(outputPath, BuildTarget.iOS, BuildTargetGroup.iOS);
    }

    // Version, defines, dev flag and the optional per-project pre-build step.
    static bool ApplyCommon(NamedBuildTarget nbt)
    {
        ApplyDefineChanges(nbt, GetArg("-defineAdd"), GetArg("-defineRemove"));

        string buildVersion = GetArg("-shipkitVersion");
        if (!string.IsNullOrEmpty(buildVersion))
        {
            PlayerSettings.bundleVersion = buildVersion;
            Console.WriteLine($"[ShipKit] bundleVersion = {buildVersion}");
        }

        EditorUserBuildSettings.development = GetArg("-shipkitDev") == "true";
        Console.WriteLine($"[ShipKit] development = {EditorUserBuildSettings.development}");

        string prebuild = GetArg("-prebuildMethod");
        if (!string.IsNullOrEmpty(prebuild))
        {
            int dot = prebuild.LastIndexOf('.');
            var type = AppDomain.CurrentDomain.GetAssemblies()
                .Select(a => a.GetType(prebuild.Substring(0, dot)))
                .FirstOrDefault(t => t != null);
            var method = type?.GetMethod(prebuild.Substring(dot + 1),
                BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
            if (method == null) { Fail($"Pre-build method not found: {prebuild}"); return false; }
            Console.WriteLine($"[ShipKit] Pre-build: {prebuild}");
            method.Invoke(null, null);
        }
        return true;
    }

    static void Build(string outputPath, BuildTarget target, BuildTargetGroup group)
    {
        string[] scenes = EditorBuildSettings.scenes.Where(s => s.enabled).Select(s => s.path).ToArray();
        if (scenes.Length == 0) { Fail("No enabled scenes in Build Settings."); return; }

        bool dev = EditorUserBuildSettings.development;
        Console.WriteLine($"[ShipKit] Output: {outputPath}");
        Console.WriteLine($"[ShipKit] Scenes ({scenes.Length}): {string.Join(", ", scenes)}");

        var report = BuildPipeline.BuildPlayer(new BuildPlayerOptions
        {
            scenes = scenes,
            locationPathName = outputPath,
            target = target,
            targetGroup = group,
            options = dev ? BuildOptions.Development | BuildOptions.AllowDebugging : BuildOptions.None,
        });

        if (report.summary.result == BuildResult.Succeeded)
        {
            Console.WriteLine($"[ShipKit] Succeeded in {report.summary.totalTime.TotalSeconds:F0}s " +
                              $"({report.summary.totalSize / 1024 / 1024} MB)");
            EditorApplication.Exit(0);
        }
        else
        {
            Console.Error.WriteLine($"[ShipKit] FAILED: {report.summary.result}  " +
                                    $"errors={report.summary.totalErrors}");
            foreach (var step in report.steps)
                foreach (var msg in step.messages)
                    if (msg.type == LogType.Error || msg.type == LogType.Exception)
                        Console.Error.WriteLine($"  {msg.content}");
            EditorApplication.Exit(1);
        }
    }

    static void Fail(string message)
    {
        Console.Error.WriteLine($"[ShipKit] ERROR: {message}");
        EditorApplication.Exit(1);
    }

    static void ApplyDefineChanges(NamedBuildTarget nbt, string addCsv, string removeCsv)
    {
        if (string.IsNullOrEmpty(addCsv) && string.IsNullOrEmpty(removeCsv)) return;
        PlayerSettings.GetScriptingDefineSymbols(nbt, out string[] current);
        var set = new HashSet<string>(current ?? Array.Empty<string>(), StringComparer.Ordinal);

        if (!string.IsNullOrEmpty(removeCsv))
            foreach (var d in removeCsv.Split(',', StringSplitOptions.RemoveEmptyEntries))
                set.Remove(d.Trim());
        if (!string.IsNullOrEmpty(addCsv))
            foreach (var d in addCsv.Split(',', StringSplitOptions.RemoveEmptyEntries))
                set.Add(d.Trim());

        var next = set.OrderBy(s => s).ToArray();
        PlayerSettings.SetScriptingDefineSymbols(nbt, next);
        Console.WriteLine($"[ShipKit] Defines ({nbt.TargetName}) = {string.Join(";", next)}");
    }

    static string GetArg(string name)
    {
        string[] args = Environment.GetCommandLineArgs();
        for (int i = 0; i < args.Length - 1; i++)
            if (args[i] == name) return args[i + 1];
        return null;
    }
}
