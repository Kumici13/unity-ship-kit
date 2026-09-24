using System;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;

/// <summary>
/// Drop into Assets/Editor/ in your Unity project.
///
/// Invoked by ship.py via:
///   Unity -batchmode -quit -executeMethod BuildScript.BuildiOS
///         -buildOutput &lt;path&gt; -buildVersion &lt;x.y&gt; -buildNumber &lt;n&gt;
///         [-developmentBuild true]
///         [-defineAdd FOO,BAR] [-defineRemove BAZ]
/// </summary>
public static class BuildScript
{
    public static void BuildiOS()
    {
        string outputPath   = GetArg("-buildOutput")     ?? "/tmp/ios-build";
        string buildVersion = GetArg("-buildVersion");
        string buildNumber  = GetArg("-buildNumber");
        bool   devBuild     = GetArg("-developmentBuild") == "true";
        string defineAdd    = GetArg("-defineAdd");
        string defineRemove = GetArg("-defineRemove");

        ApplyDefineChanges(defineAdd, defineRemove);

        if (!string.IsNullOrEmpty(buildVersion))
        {
            PlayerSettings.bundleVersion = buildVersion;
            Console.WriteLine($"[BuildScript] bundleVersion = {buildVersion}");
        }

        if (!string.IsNullOrEmpty(buildNumber))
        {
            PlayerSettings.iOS.buildNumber = buildNumber;
            Console.WriteLine($"[BuildScript] iOS.buildNumber = {buildNumber}");
        }

        EditorUserBuildSettings.development = devBuild;
        Console.WriteLine($"[BuildScript] Development build = {devBuild}");

        string[] scenes = EditorBuildSettings.scenes
            .Where(s => s.enabled)
            .Select(s => s.path)
            .ToArray();

        if (scenes.Length == 0)
        {
            Console.Error.WriteLine("[BuildScript] ERROR: No enabled scenes in Build Settings.");
            EditorApplication.Exit(1);
            return;
        }

        Console.WriteLine($"[BuildScript] Output: {outputPath}");
        Console.WriteLine($"[BuildScript] Scenes ({scenes.Length}): {string.Join(", ", scenes)}");

        var buildOptions = devBuild
            ? BuildOptions.Development | BuildOptions.AllowDebugging
            : BuildOptions.None;

        var report = BuildPipeline.BuildPlayer(new BuildPlayerOptions
        {
            scenes = scenes,
            locationPathName = outputPath,
            target = BuildTarget.iOS,
            options = buildOptions,
        });

        if (report.summary.result == BuildResult.Succeeded)
        {
            Console.WriteLine($"[BuildScript] Succeeded in {report.summary.totalTime.TotalSeconds:F0}s");
            EditorApplication.Exit(0);
        }
        else
        {
            Console.Error.WriteLine($"[BuildScript] FAILED: {report.summary.result}  " +
                                    $"errors={report.summary.totalErrors}");
            foreach (var step in report.steps)
                foreach (var msg in step.messages)
                    if (msg.type == LogType.Error || msg.type == LogType.Exception)
                        Console.Error.WriteLine($"  {msg.content}");
            EditorApplication.Exit(1);
        }
    }

    static void ApplyDefineChanges(string addCsv, string removeCsv)
    {
        if (string.IsNullOrEmpty(addCsv) && string.IsNullOrEmpty(removeCsv)) return;
        var nbt = NamedBuildTarget.iOS;
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
        Console.WriteLine($"[BuildScript] Defines (iOS) = {string.Join(";", next)}");
    }

    static string GetArg(string name)
    {
        string[] args = Environment.GetCommandLineArgs();
        for (int i = 0; i < args.Length - 1; i++)
            if (args[i] == name) return args[i + 1];
        return null;
    }
}
