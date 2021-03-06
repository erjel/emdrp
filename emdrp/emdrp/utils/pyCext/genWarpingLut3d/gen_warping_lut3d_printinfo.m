
% just for debug, prints percentages of each of the types of points in the LUT

STR_NONSIMPLE = {...
  'simple',...
  '0=>1 create object (missing object)','0=>1 delete cavity (extra cavity)',...
  '0=>1 merger (split objects)','delete tunnel (extra tunnel)',...
  '1=>0 create cavity (missing cavity)','1=>0 delete object (extra object)',...
  '1=>0 split (merged objects)','create tunnel (missing tunnel)'};


for i=0:8
  a=sum(simpleLUT6_26==i)/2^27;
  fprintf(1,'%g \t\t%% %s\n',a,STR_NONSIMPLE{i+1});
end
