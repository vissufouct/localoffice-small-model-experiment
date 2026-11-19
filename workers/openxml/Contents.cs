using DocumentFormat.OpenXml;
using W = DocumentFormat.OpenXml.Wordprocessing;

static class Contents
{
    // A native complex field, not manually generated page numbers. Its cache is
    // deliberately labelled until a pagination engine updates it.
    internal static W.Paragraph Create(int maxLevel, bool pageBreak)
    {
        if(maxLevel<1 || maxLevel>6)throw new InvalidDataException("Invalid TOC heading level");
        var properties=new W.ParagraphProperties(new W.ParagraphStyleId { Val="Normal" });
        if(pageBreak)properties.AddChild(new W.PageBreakBefore());
        return new W.Paragraph(properties,
            new W.Run(new W.FieldChar { FieldCharType=W.FieldCharValues.Begin,Dirty=true }),
            new W.Run(new W.FieldCode($" TOC \\o \"1-{maxLevel}\" \\h ") { Space=SpaceProcessingModeValues.Preserve }),
            new W.Run(new W.FieldChar { FieldCharType=W.FieldCharValues.Separate }),
            new W.Run(new W.Text("Table of contents requires a field update.")),
            new W.Run(new W.FieldChar { FieldCharType=W.FieldCharValues.End }));
    }
}
