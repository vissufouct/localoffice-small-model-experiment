using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using DocumentFormat.OpenXml;
using W=DocumentFormat.OpenXml.Wordprocessing;

// Narrow replace_text adapter. Field caches count towards Program.Text offsets,
// but only literal text nodes outside fields can be changed. No field rebuilds.
static class FieldLiteralText
{
    const string Wns="http://schemas.openxmlformats.org/wordprocessingml/2006/main";
    const string Xmlns="http://www.w3.org/XML/1998/namespace";
    static void Require(bool condition,string message)
    { if(!condition)throw new InvalidDataException(message); }
    static int Length(string value)=>value.EnumerateRunes().Count();
    static string Slice(string value,int start,int count)=>string.Concat(value.EnumerateRunes().Skip(start).Take(count).Select(r=>r.ToString()));

    // Descendants, not just direct children: nested unsupported fields must reach
    // this guard and fail closed rather than fall through to generic replacement.
    internal static bool Handles(W.Paragraph paragraph)=>paragraph.Descendants<W.SimpleField>().Any();

    static void CheckVocabulary(OpenXmlElement element)
    {
        foreach(var node in new[]{element}.Concat(element.Descendants()))
        {
            Require(node.NamespaceUri==Wns && node is not OpenXmlUnknownElement &&
                node.LocalName is not ("pPrChange" or "rPrChange" or "ins" or "del" or "moveFrom" or "moveTo"),
                "Unsupported field-literal structure: extensions and tracked changes are read-only");
            Require(node.GetAttributes().All(a=>a.NamespaceUri==Wns ||
                (node is W.Text && a.NamespaceUri==Xmlns && a.LocalName=="space")),
                "Unsupported field-literal attributes or extensions");
        }
    }
    static void CheckProperties(OpenXmlElement properties)
    {
        // Formatting is retained verbatim; it must not conceal text/field content
        // that would disagree with the text projection used by the caller.
        Require(!properties.Descendants().Any(n=>n is W.Text or W.Run or W.SimpleField or W.FieldCode or W.FieldChar or W.Hyperlink or W.Drawing or W.TabChar or W.Break),
            "Unsupported content inside field-literal formatting");
    }
    static void CheckRun(W.Run run)
    {
        Require(run.ChildElements.All(c=>c is W.RunProperties or W.Text) &&
            run.Elements<W.RunProperties>().Count()<=1 &&
            (run.RunProperties==null || ReferenceEquals(run.FirstChild,run.RunProperties)),
            "Unsupported field-literal run: only cached or literal text with formatting is allowed");
        if(run.RunProperties!=null)CheckProperties(run.RunProperties);
        Require(run.Elements<W.Text>().All(t=>!t.HasChildren),"Unsupported field-literal text children");
    }

    internal static void Apply(W.Paragraph paragraph,JsonElement operation)
    {
        Require(Handles(paragraph),"No simple page fields found for field-literal replacement");
        CheckVocabulary(paragraph);
        Require(paragraph.ChildElements.All(c=>c is W.ParagraphProperties or W.Run or W.SimpleField) &&
            paragraph.Elements<W.ParagraphProperties>().Count()<=1 &&
            (paragraph.ParagraphProperties==null || ReferenceEquals(paragraph.FirstChild,paragraph.ParagraphProperties)),
            "Unsupported field-literal paragraph: only properties, text runs and simple page fields are allowed");
        if(paragraph.ParagraphProperties!=null)CheckProperties(paragraph.ParagraphProperties);
        var literals=new List<(W.Text node,int start,int end)>();
        var fields=new List<(int start,int end)>();
        int offset=0;
        foreach(var child in paragraph.ChildElements)
        {
            if(child is W.Run run)
            {
                CheckRun(run);
                foreach(var text in run.Elements<W.Text>())
                {
                    int end=offset+Length(text.Text);literals.Add((text,offset,end));offset=end;
                }
            }
            else if(child is W.SimpleField field)
            {
                Require(Regex.IsMatch(field.Instruction?.Value??"",@"\A\s*(PAGE|NUMPAGES)\s*\z",RegexOptions.IgnoreCase|RegexOptions.CultureInvariant),
                    "Only plain PAGE and NUMPAGES simple fields support literal edits");
                Require(field.GetAttributes().All(a=>a.NamespaceUri==Wns && a.LocalName is "instr" or "fldLock" or "dirty") &&
                    field.ChildElements.All(c=>c is W.Run),
                    "Unsupported simple page field: nested fields, complex fields and extensions are read-only");
                int start=offset;
                foreach(var cached in field.Elements<W.Run>())
                { CheckRun(cached);offset+=cached.Elements<W.Text>().Sum(t=>Length(t.Text)); }
                fields.Add((start,offset));
            }
        }
        Require(operation.TryGetProperty("start",out var startValue) && startValue.TryGetInt32(out _) &&
            operation.TryGetProperty("end",out var endValue) && endValue.TryGetInt32(out _) &&
            operation.TryGetProperty("text",out var textValue) && textValue.ValueKind==JsonValueKind.String,
            "Invalid field-literal replacement arguments");
        int from=operation.GetProperty("start").GetInt32(),to=operation.GetProperty("end").GetInt32();
        string replacement=operation.GetProperty("text").GetString()!;
        Require(from>=0 && to>=from && to<=offset,"Invalid field-literal text span");
        Require(Length(replacement)<=8000 && !replacement.Any(c=>char.IsControl(c)) &&
            !replacement.Any(c=>c is '\uFFFE' or '\uFFFF'),"Field-literal replacement requires XML-safe text without control characters");
        // Json decoding can retain escaped lone surrogates. Never silently replace
        // malformed Unicode while calculating codepoint offsets or writing XML.
        for(int i=0;i<replacement.Length;i++)
            if(char.IsSurrogate(replacement[i]))
            { Require(char.IsHighSurrogate(replacement[i]) && i+1<replacement.Length && char.IsLowSurrogate(replacement[i+1]),"Invalid Unicode in field-literal replacement");i++; }
        if(from==to)
            Require(!fields.Any(f=>from>=f.start && from<=f.end),"Insertion inside or on a field boundary is ambiguous");
        else
            Require(!fields.Any(f=>(from<f.end && to>f.start) || (f.start==f.end && from<f.start && to>f.end)),
                "Replacement touches or crosses a field; only literal text spans are editable");
        var selected=literals.Where(t=>t.end>t.start && (from==to?from>=t.start && from<=t.end:to>t.start && from<t.end)).ToList();
        Require(selected.Count>0,"No safe literal range found");
        // Identical text is a genuine no-op even when the span crosses multiple
        // differently formatted runs: do not consolidate text into the first run.
        if(from!=to)
        {
            var current=string.Concat(selected.Select(t=>Slice(t.node.Text,Math.Max(from-t.start,0),Math.Min(to,t.end)-Math.Max(from,t.start))));
            if(current==replacement)return;
        }
        var changes=new List<(W.Text node,string value)>();bool inserted=false;
        foreach(var literal in selected)
        {
            if(from==to && inserted)break;
            int length=literal.end-literal.start;
            int left=Math.Clamp(from-literal.start,0,length),right=Math.Clamp(to-literal.start,0,length);
            string updated=Slice(literal.node.Text,0,left)+(inserted?"":replacement)+Slice(literal.node.Text,right,length-right);
            if(updated!=literal.node.Text)changes.Add((literal.node,updated));
            inserted=true;
        }
        // Everything was checked before touching the original tree. Preserve all
        // run/property/field nodes, and only adjust selected literal w:t values.
        foreach(var (node,value) in changes){node.Text=value;node.Space=SpaceProcessingModeValues.Preserve;}
    }
}
