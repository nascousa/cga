import * as echarts from 'echarts/core';
import {BarChart, LineChart, PieChart} from 'echarts/charts';
import {GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, AriaComponent} from 'echarts/components';
import {SVGRenderer} from 'echarts/renderers';

echarts.use([BarChart, LineChart, PieChart, GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, AriaComponent, SVGRenderer]);
export {echarts};

export async function exportLibraries() {
  const [{default: mermaid}, {default: html2canvas}, {jsPDF}] = await Promise.all([
    import('mermaid'), import('html2canvas'), import('jspdf'),
  ]);
  return {mermaid, html2canvas, jsPDF};
}
