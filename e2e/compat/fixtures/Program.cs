using System.Text.Json;
using Jellyfin.Plugin.RefreshKit;

internal sealed record FixtureDocument(int SchemaVersion, IReadOnlyList<StampFixture> Cases);

internal sealed record StampFixture(
    string Id,
    string Generation,
    string Html,
    int ExpectedStampCount,
    IReadOnlyList<string> MustContain,
    IReadOnlyList<string> MustNotContain);

internal static class Program
{
    private const string OwnMarker = "plugin=\"Jellyfin Refresh Kit\"";

    public static int Main(string[] args)
    {
        if (args.Length != 1)
        {
            Console.Error.WriteLine("usage: StaticFixtureHarness <stamping.json>");
            return 2;
        }

        var options = new JsonSerializerOptions
        {
            PropertyNameCaseInsensitive = true,
        };
        var source = File.ReadAllText(args[0]);
        var document = JsonSerializer.Deserialize<FixtureDocument>(source, options)
            ?? throw new InvalidOperationException("fixture document deserialized to null");
        if (document.SchemaVersion != 1 || document.Cases.Count == 0)
        {
            throw new InvalidOperationException("fixture document must be non-empty schemaVersion 1");
        }

        var ids = new HashSet<string>(StringComparer.Ordinal);
        var results = new List<object>();
        foreach (var fixture in document.Cases)
        {
            if (!ids.Add(fixture.Id))
            {
                throw new InvalidOperationException($"duplicate fixture id: {fixture.Id}");
            }

            var output = ThirdPartyTagStamper.Stamp(fixture.Html, fixture.Generation, OwnMarker);
            var secondPass = ThirdPartyTagStamper.Stamp(output, fixture.Generation, OwnMarker);
            if (!string.Equals(output, secondPass, StringComparison.Ordinal))
            {
                throw new InvalidOperationException($"{fixture.Id}: stamping is not idempotent");
            }

            var escapedGeneration = Uri.EscapeDataString(fixture.Generation);
            var stampCount = Count(output, "rkv=" + escapedGeneration);
            if (stampCount != fixture.ExpectedStampCount)
            {
                throw new InvalidOperationException(
                    $"{fixture.Id}: expected {fixture.ExpectedStampCount} stamps, found {stampCount}\n{output}");
            }

            foreach (var required in fixture.MustContain)
            {
                if (!output.Contains(required, StringComparison.Ordinal))
                {
                    throw new InvalidOperationException(
                        $"{fixture.Id}: required output fragment missing: {required}\n{output}");
                }
            }

            foreach (var forbidden in fixture.MustNotContain)
            {
                if (output.Contains(forbidden, StringComparison.Ordinal))
                {
                    throw new InvalidOperationException(
                        $"{fixture.Id}: forbidden output fragment present: {forbidden}\n{output}");
                }
            }

            results.Add(new
            {
                fixture.Id,
                fixture.ExpectedStampCount,
                Idempotent = true,
                Passed = true,
            });
        }

        Console.WriteLine(JsonSerializer.Serialize(new
        {
            SchemaVersion = 1,
            Cases = results,
            AllPassed = true,
        }, new JsonSerializerOptions { WriteIndented = true }));
        return 0;
    }

    private static int Count(string source, string needle)
    {
        var count = 0;
        var offset = 0;
        while ((offset = source.IndexOf(needle, offset, StringComparison.Ordinal)) >= 0)
        {
            count++;
            offset += needle.Length;
        }

        return count;
    }
}
