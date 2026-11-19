using DocumentFormat.OpenXml;
using W=DocumentFormat.OpenXml.Wordprocessing;

static class ParagraphDeletion
{
    internal static void Apply(OpenXmlElement target, W.Document document)
    {
        if(target is not W.Paragraph paragraph || (target.Parent is not W.Body && target.Parent is not W.TableCell))
            throw new InvalidDataException("Deletion requires a direct body or table-cell paragraph");
        if(paragraph.Descendants<W.SectionProperties>().Any() ||
           paragraph.Descendants<W.ParagraphPropertiesChange>().Any() || paragraph.Descendants<W.RunPropertiesChange>().Any() ||
           paragraph.Descendants().Any(e=>e.NamespaceUri==paragraph.NamespaceUri && new[]{"ins","del","moveFrom","moveTo"}.Contains(e.LocalName)) ||
           !paragraph.ChildElements.All(e=>e is W.ParagraphProperties or W.Run) ||
           paragraph.Elements<W.Run>().Any(r=>!r.ChildElements.All(e=>e is W.RunProperties or W.Text or W.TabChar or W.Break)))
            throw new InvalidDataException("Paragraph deletion preserves section breaks, fields, markers, drawings and tracked changes; this target requires additional support");
        // A simple-looking paragraph can be inside a field spanning paragraphs.
        int depth=0;
        foreach(var element in document.Descendants())
        {
            if(ReferenceEquals(element,paragraph))break;
            if(element is W.FieldChar field)
            {
                if(field.FieldCharType?.Value==W.FieldCharValues.Begin)depth++;
                else if(field.FieldCharType?.Value==W.FieldCharValues.End)
                {
                    if(depth==0)throw new InvalidDataException("Unbalanced field before deletion target");
                    depth--;
                }
            }
        }
        if(depth!=0)throw new InvalidDataException("Cannot delete a paragraph inside a spanning field");
        var parent=paragraph.Parent!;
        if(parent.Elements<W.Paragraph>().Count()<=1)
            throw new InvalidDataException("Preserve the last required paragraph in the body or cell");
        if(parent is W.TableCell && ReferenceEquals(parent.LastChild,paragraph) && paragraph.PreviousSibling() is not W.Paragraph)
            throw new InvalidDataException("Preserve the required paragraph after a nested table");
        if(paragraph.PreviousSibling() is W.Table && paragraph.NextSibling() is W.Table)
            throw new InvalidDataException("Deleting this paragraph would join adjacent tables");
        paragraph.Remove();
    }
}
