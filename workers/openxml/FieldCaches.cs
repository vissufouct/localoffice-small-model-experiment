using DocumentFormat.OpenXml;
using DocumentFormat.OpenXml.Packaging;
using System.Text.RegularExpressions;
using W=DocumentFormat.OpenXml.Wordprocessing;

static class FieldCaches
{
    static void Check(bool ok,string message){if(!ok)throw new InvalidDataException(message);}
    static bool Toc(OpenXmlElement node)=>node.Descendants<W.FieldCode>().Any(c=>Regex.IsMatch(c.Text,@"^\s*TOC\b",RegexOptions.IgnoreCase));
    static OpenXmlElement Container(W.Document document)
    {
        var found=document.Body!.ChildElements.Where(Toc).ToList();
        Check(found.Count==1,"Field refresh requires one top-level TOC container");
        var node=found[0];
        Check(node is W.Paragraph || node is W.SdtBlock,"Unsupported TOC container");
        Check(node.Descendants<W.FieldCode>().Count(c=>Regex.IsMatch(c.Text,@"^\s*TOC\b",RegexOptions.IgnoreCase))==1,"Ambiguous TOC fields");
        Check(!node.Descendants<W.Table>().Any() && !node.Descendants<W.SectionProperties>().Any(),"Unsupported TOC content");
        var fieldChars=node.Descendants<W.FieldChar>().ToList();
        Check(fieldChars.Count>=3 && fieldChars[0].FieldCharType?.Value==W.FieldCharValues.Begin && fieldChars[^1].FieldCharType?.Value==W.FieldCharValues.End,"Unsupported TOC boundaries");
        // No title or other content may be swept into replacement of this container.
        bool started=false,ended=false;
        foreach(var leaf in node.Descendants())
        {
            if(ReferenceEquals(leaf,fieldChars[0]))started=true;
            if(leaf is W.Text t && t.Text.Length>0)Check(started&&!ended,"Text outside TOC field boundary");
            if(ReferenceEquals(leaf,fieldChars[^1]))ended=true;
        }
        return node;
    }
    static string Text(W.Paragraph paragraph)=>PageFields.Projection(paragraph);
    static List<W.Paragraph> Outside(W.Document document,OpenXmlElement toc)=>document.Descendants<W.Paragraph>()
        .Where(p=>!ReferenceEquals(p,toc)&&!p.Ancestors().Contains(toc)).ToList();

    internal static void Apply(W.Document document,W.Styles styles,WordprocessingDocument refreshed)
    {
        var source=Container(document);var updated=refreshed.MainDocumentPart?.Document ?? throw new InvalidDataException("Missing refreshed document");
        Check(!source.Descendants().Any(e=>e.LocalName is "ins" or "del" or "moveFrom" or "moveTo" or
            "bookmarkStart" or "bookmarkEnd" or "commentRangeStart" or "commentRangeEnd" or "commentReference" or
            "footnoteReference" or "endnoteReference" or "drawing" || e.LocalName.EndsWith("PrChange")),
            "TOC contains annotations, tracked content or embedded objects; cache transfer refused");
        var generated=Container(updated);
        var originalParagraphs=Outside(document,source);var renderedParagraphs=Outside(updated,generated);
        Check(originalParagraphs.Count==renderedParagraphs.Count,"Renderer changed paragraph topology; cache transfer refused");
        for(int i=0;i<originalParagraphs.Count;i++)
            Check(Text(originalParagraphs[i])==Text(renderedParagraphs[i]),"Renderer changed paragraph content; cache transfer refused");
        var replacement=generated.CloneNode(true);
        if(source is W.SdtBlock originalControl && generated is W.SdtBlock refreshedControl)
        {
            var retained=(W.SdtBlock)originalControl.CloneNode(true);
            retained.SdtContentBlock=(W.SdtContentBlock)refreshedControl.SdtContentBlock!.CloneNode(true);
            replacement=retained;
        }
        Check(replacement.Descendants<W.Paragraph>().Count()<=2000,"TOC cache exceeds 2000 entries");
        Check(!replacement.Descendants().SelectMany(e=>e.GetAttributes()).Any(a=>a.NamespaceUri=="http://schemas.openxmlformats.org/officeDocument/2006/relationships"),"TOC cache has external part dependencies");
        var oldCode=source.Descendants<W.FieldCode>().First(c=>Regex.IsMatch(c.Text,@"^\s*TOC\b",RegexOptions.IgnoreCase)).Text;
        var newCode=replacement.Descendants<W.FieldCode>().First(c=>Regex.IsMatch(c.Text,@"^\s*TOC\b",RegexOptions.IgnoreCase));
        Check(Regex.Replace(oldCode,@"\s+"," ").Trim()==Regex.Replace(newCode.Text,@"\s+"," ").Trim(),"Renderer changed TOC options");
        newCode.Text=oldCode;
        // Local page-break placement belongs to the source, not the reserializer.
        var originalFirst=source as W.Paragraph ?? source.Descendants<W.Paragraph>().First();
        var newFirst=replacement as W.Paragraph ?? replacement.Descendants<W.Paragraph>().First();
        if(originalFirst.ParagraphProperties?.PageBreakBefore is W.PageBreakBefore br)
        {
            newFirst.ParagraphProperties ??=new W.ParagraphProperties();
            newFirst.ParagraphProperties.PageBreakBefore=(W.PageBreakBefore)br.CloneNode(true);
        }
        var names=document.Descendants<W.BookmarkStart>().Select(b=>b.Name?.Value??"").ToHashSet();
        var ids=document.Descendants<W.BookmarkStart>().Select(b=>long.TryParse(b.Id?.Value,out long value)?value:-1).ToList();
        Check(ids.All(id=>id>=0 && id<int.MaxValue-2000),"Bookmark identifiers exceed the supported range");
        long next=ids.DefaultIfEmpty(-1).Max()+1;
        var previousLinks=source.Descendants<W.Hyperlink>().Select(h=>h.Anchor?.Value??"").ToHashSet();
        var targetMap=new Dictionary<string,string>();
        foreach(var link in replacement.Descendants<W.Hyperlink>())
        {
            var anchor=link.Anchor?.Value;Check(!string.IsNullOrEmpty(anchor),"TOC link is not a local anchor");
            if(!targetMap.TryGetValue(anchor!,out var assigned))
            {
                var matches=renderedParagraphs.Select((p,i)=>(p,i)).Where(x=>x.p.Descendants<W.BookmarkStart>().Any(b=>b.Name?.Value==anchor)).ToList();
                Check(matches.Count==1,"TOC destination is missing or ambiguous");
                var paragraph=originalParagraphs[matches[0].i];
                var reusable=paragraph.Elements<W.BookmarkStart>().FirstOrDefault(b=>b.Name?.Value?.StartsWith("_LocalOfficeToc")==true && previousLinks.Contains(b.Name.Value) &&
                    b.NextSibling() is W.BookmarkEnd end && end.Id?.Value==b.Id?.Value);
                if(reusable!=null)assigned=reusable.Name!.Value!;
                else
                {
                    do {assigned="_LocalOfficeToc"+next++;} while(names.Contains(assigned));
                    names.Add(assigned);string id=(next-1).ToString();
                    var start=new W.BookmarkStart { Name=assigned,Id=id };var end=new W.BookmarkEnd { Id=id };
                    if(paragraph.ParagraphProperties!=null)paragraph.InsertAfter(start,paragraph.ParagraphProperties);else paragraph.PrependChild(start);
                    paragraph.InsertAfter(end,start);
                }
                targetMap[anchor!]=assigned;
            }
            link.Anchor=assigned;
        }
        Check(!replacement.Descendants<W.BookmarkStart>().Any(),"Unexpected bookmarks inside generated TOC");
        var generatedStyles=refreshed.MainDocumentPart!.StyleDefinitionsPart?.Styles ?? throw new InvalidDataException("Missing generated TOC styles");
        var pending=new Queue<string>(replacement.Descendants<W.ParagraphStyleId>().Select(s=>s.Val!.Value!).Concat(replacement.Descendants<W.RunStyle>().Select(s=>s.Val!.Value!)));
        var known=styles.Elements<W.Style>().Select(s=>s.StyleId!.Value!).ToHashSet();
        while(pending.TryDequeue(out var id))
        {
            if(!known.Add(id))continue;
            Check(known.Count<10000,"Style dependency limit");
            var definition=generatedStyles.Elements<W.Style>().SingleOrDefault(s=>s.StyleId?.Value==id) ?? throw new InvalidDataException("Missing TOC style dependency");
            Check(!definition.Descendants<W.NumberingProperties>().Any(),"TOC style has numbering dependency");
            var copy=(W.Style)definition.CloneNode(true);copy.Default=null;styles.Append(copy);
            foreach(var dep in new[]{definition.BasedOn?.Val?.Value,definition.NextParagraphStyle?.Val?.Value,definition.LinkedStyle?.Val?.Value})
                if(dep!=null)pending.Enqueue(dep);
        }
        source.InsertAfterSelf(replacement);source.Remove();
    }
}
