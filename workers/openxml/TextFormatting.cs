using System.Text.Json;
using DocumentFormat.OpenXml;
using W=DocumentFormat.OpenXml.Wordprocessing;

static class TextFormatting
{
    static bool Marker(OpenXmlElement e)=>e is W.BookmarkStart or W.BookmarkEnd or W.ProofError;
    static int Size(OpenXmlElement e)=>e switch { W.Text t=>t.Text.EnumerateRunes().Count(), W.TabChar or W.Break=>1,_=>0 };
    static string Slice(string value,int start,int length)=>string.Concat(value.EnumerateRunes().Skip(start).Take(length).Select(r=>r.ToString()));
    static W.Run Portion(W.Run source,int start,int end)
    {
        var result=(W.Run)source.CloneNode(false);
        if(source.RunProperties!=null)result.Append(source.RunProperties.CloneNode(true));
        int offset=0;
        foreach(var node in source.ChildElements.Where(e=>e is not W.RunProperties))
        {
            int length=Size(node),left=Math.Max(start-offset,0),right=Math.Min(end-offset,length);
            if(right>left)
            {
                var copy=node.CloneNode(true);
                if(copy is W.Text text && (left!=0 || right!=length))
                { text.Text=Slice(text.Text,left,right-left);text.Space=SpaceProcessingModeValues.Preserve; }
                result.Append(copy);
            }
            offset+=length;
        }
        return result;
    }
    static void Typography(W.Run run,JsonElement properties)
    {
        if(properties.EnumerateObject().Any(p=>!new[]{"font","size_points","size_half_points","color","bold","italic"}.Contains(p.Name)))
            throw new InvalidDataException("Text spans accept typography only");
        // Reuse existing typography semantics without applying paragraph layout.
        var container=new W.Paragraph(run);Formatting.Apply(container,properties);run.Remove();
    }
    internal static void Apply(W.Paragraph paragraph,JsonElement op,Action<W.Run>? format=null)
    {
        if(!paragraph.ChildElements.All(e=>e is W.ParagraphProperties or W.Run or W.Hyperlink || Marker(e)) ||
            paragraph.Elements<W.Hyperlink>().Any(h=>!h.ChildElements.All(e=>e is W.Run || Marker(e))))
            throw new InvalidDataException("Unsupported paragraph structure for span formatting");
        var runs=paragraph.Descendants<W.Run>().ToList();
        if(runs.Any(r=>!r.ChildElements.All(e=>e is W.RunProperties or W.Text or W.TabChar or W.Break)))
            throw new InvalidDataException("Unsupported run content for span formatting; preserve fields/drawings/tracked changes");
        int start=op.GetProperty("start").GetInt32(),end=op.GetProperty("end").GetInt32();
        int total=runs.Sum(r=>r.ChildElements.Sum(Size));
        if(start<0 || end<=start || end>total)throw new InvalidDataException("Invalid nonempty text span");
        format ??= run=>Typography(run,op.GetProperty("properties"));int offset=0;
        foreach(var run in runs)
        {
            int length=run.ChildElements.Sum(Size),left=Math.Max(start-offset,0),right=Math.Min(end-offset,length);offset+=length;
            if(right<=left)continue;
            var formatted=(W.Run)run.CloneNode(true);format(formatted);
            if(formatted.OuterXml==run.OuterXml)continue;
            if(left==0 && right==length){run.InsertBeforeSelf(formatted);run.Remove();continue;}
            if(left>0)run.InsertBeforeSelf(Portion(run,0,left));
            var middle=Portion(run,left,right);format(middle);run.InsertBeforeSelf(middle);
            if(right<length)run.InsertBeforeSelf(Portion(run,right,length));
            run.Remove();
        }
    }
}
