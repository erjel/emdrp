
function volume_diameter_fit

h5mesh = '~/Downloads/K0057_soma_annotation/out/K0057-D31-somas_dsx12y12z4-clean-cut-fit-ellipses.0.mesh.h5';
h5vol = '~/Downloads/K0057_soma_annotation/out/K0057-D31-somas_dsx12y12z4-clean-cut-fit-ellipses.h5';
bounds_mat = '~/Downloads/K0057_soma_annotation/out/soma_bounds_fit_ellipses.mat';
outcutfile = '~/Downloads/K0057_soma_annotation/out/soma_cuts_fit_ellipses.mat';

ncuts = 0;
smoothdia = 500; % nm, smoothing window size for diameters
slope_cut = 0.5; % fudge factor for determining point to cut
cut_axis = 3; % which eigenaxis to cut along
dataset_root = '0';
doplots = true;
getbounds = falses;

scale = h5readatt(h5vol,'/labels','scale')';
minnormal = 1e-2;
dx = scale/5;

if getbounds
  % read the whole volume and get bounding boxes
  Vlbls = h5read(h5vol, '/labels'); nseeds = double(max(Vlbls(:)));
  [label_mins, label_maxs] = find_objects_mex(uint32(Vlbls), uint32(nseeds)); clear Vlbls
  save(bounds_mat, 'label_mins', 'label_maxs', 'nseeds');
else
  load(bounds_mat);
end

info = h5info(h5mesh);
nseeds = length({info.Groups(1).Groups.Name})-1;

cut_n = zeros(nseeds,3); cut_d = inf(nseeds,2);
rcut_n = zeros(3,1); rcut_n(cut_axis) = 1;
for seed=1:nseeds
%for seed=710:nseeds
  t = now;
  
  % load the meshes
  seed_root = sprintf('/%s/%08d',dataset_root,seed);
  faces = h5read(h5mesh, [seed_root '/faces'])' + 1;
  vertices = double(h5read(h5mesh, [seed_root '/vertices'])'); nverts = size(vertices,1);
  bounds_beg = double(h5readatt(h5mesh, [seed_root '/vertices'], 'bounds_beg')');
  % vertex coordinates relative to entire dataset
  vertices = bsxfun(@plus, vertices, bounds_beg);

  % load the volume
  pmin = double(label_mins(:,seed)'); pmax = double(label_maxs(:,seed)');
  corner = double(pmin); rng = double(pmax - pmin + 1); srng = rng.*scale;
  Vlbls = h5read(h5vol, '/labels', corner, rng); bwcrp = (Vlbls == seed);

  % get the coordinates of the points for this label, relative to bounding box of the label
  [x,y,z] = ind2sub(rng,find(bwcrp(:)));
  vpts = bsxfun(@times,[x y z]-0.5,scale);

  % correct vertices to be relative to bounding box of label
  vertices = bsxfun(@minus, vertices, (corner-1).*scale);
  
  % what points to use for svd, mesh aligns any "processes" better along principal eigenaxis
  pts = vertices; C = mean(pts,1); Cpts = bsxfun(@minus,pts,C);
  
  % svd on centered points to get principal axes.
  % in matlab V is returned normal (not transposed), so "eigenvectors" are along columns,
  %   i.e. V(:,1) V(:,2) V(:,3)
  %[~,S,V] = svd(Cpts,0); s = sqrt(diag(S).^2/(npts-1));
  [~,~,V] = svd(Cpts,0);
  
  % rotate the points to align on cartesian axes
  rvertices = bsxfun(@plus,(V'*bsxfun(@minus,vertices',C')),C')';
  rpts = bsxfun(@plus,(V'*bsxfun(@minus,vpts',C')),C')';
  rC = mean(rpts,1);

  selmin = true(nverts,1);
  if ncuts > 0
    % march plane along principal eigen axis and measure diameter
    mind = min( rpts ); maxd = max( rpts ); step = ( maxd - mind ) / (ncuts+1); 
    cut_pts = repmat( rC, [ncuts 1] );
    cut_pts(:,cut_axis) = linspace( mind(cut_axis) + step(cut_axis), maxd(cut_axis) - step(cut_axis), ncuts );
    
    % rotate cutting plane points back to original frame
    rcut_pts = bsxfun(@plus,(V*bsxfun(@minus,cut_pts',C')),C')';
    % calculate the plane offsets using the cutting points
    normal = (V*rcut_n)'; d = -sum(bsxfun(@times,normal,rcut_pts),2);
    cut_n(seed,:) = normal;

    diameters = nan(1,ncuts);
    for i=1:ncuts
        % create the plane orthgonal to the edge within cropped area
        assert(any(abs(normal)>minnormal)); [~,j] = max(abs(normal));
        if j==3
          [xx,yy] = ndgrid(0:dx(1):srng(1),0:dx(2):srng(2));
          zz = -(normal(1)*xx + normal(2)*yy + d(i))/normal(3);
        elseif j==2
          [xx,zz] = ndgrid(0:dx(1):srng(1),0:dx(3):srng(3));
          yy = -(normal(1)*xx + normal(3)*zz + d(i))/normal(2);
        else
          [yy,zz] = ndgrid(0:dx(2):srng(2),0:dx(3):srng(3));
          xx = -(normal(2)*yy + normal(3)*zz + d(i))/normal(1);
        end

        % rasterize the plane
        plane_subs = round(bsxfun(@rdivide,[xx(:) yy(:) zz(:)],scale));
        % remove out of bounds
        plane_subs = plane_subs(~any(bsxfun(@gt, plane_subs, rng),2),:); 
        plane_subs = plane_subs(~any(plane_subs < 1,2),:);         
        plane_sel = false(rng); plane_sel(sub2ind(rng,plane_subs(:,1),plane_subs(:,2),plane_subs(:,3))) = true;

        % intersect rasterized plane with this label. take largest component for diameter
        bwp = regionprops(bwcrp & plane_sel,'basic'); [~,j] = max([bwp.Area]);
        if ~isempty(bwp)
          % get diameter as euclidean distance across the bounding box range
          diameters(i) = sqrt(sum((bwp(j).BoundingBox(4:6).*scale).^2));
        end
    end
    
    % smooth the diameters
    box = ceil(smoothdia/step(cut_axis)); if mod(box,2)==0, box=box+1; end 
    fdelay = (box-1)/2; xdiameters = (1:length(diameters))*step(cut_axis);
    sdiameters = nan(1,length(diameters)); dsdiameters = nan(1,length(diameters)); 
    tmp = filter(ones(1, box)/box, 1, diameters); 
    sdiameters(1:end-fdelay) = tmp(fdelay+1:end);
    % smooth the derivative of diameters
    tmp = filter(ones(1, box)/box, 1, diff(sdiameters)/step(cut_axis)); 
    dsdiameters(1:end-fdelay-1) = tmp(fdelay+1:end);

    % take only the main diameter peak of the soma
    cutL = 0; cutR = 0;
    [m,j] = max(sdiameters); sel = (sdiameters < m/3);
    k = find((dsdiameters(j:-1:1) <= slope_cut) & sel(j:-1:1),1);
    if ~isempty(k)
      cutL = j-k+2; 
      cut_d(seed,1) = d(cutL);
      selmin = selmin & (sum(bsxfun(@times,vertices,cut_n(seed,:)),2) + cut_d(seed,1) > 0);
    end
    k = find((dsdiameters(j:end) >= -slope_cut) & sel(j:end),1);
    if ~isempty(k)
      cutR = j+k-2; 
      cut_d(seed,2) = d(cutR);
      selmin = selmin & (sum(bsxfun(@times,vertices,cut_n(seed,:)),2) + cut_d(seed,2) < 0);
    end
  end
  
  if doplots
    plot_pts_surf(faces, rvertices, selmin);
    
    if ncuts > 0
      figure(1235);clf
      [ax,hLine1,hLine2] = plotyy(xdiameters, sdiameters, xdiameters, dsdiameters); hold on
      if cutL > 0
        plot([xdiameters(cutL) xdiameters(cutL)], [0 max(sdiameters)], 'r');
      end
      if cutR > 0
        plot([xdiameters(cutR) xdiameters(cutR)], [0 max(sdiameters)], 'r');
      end
      
      hLine1.Marker = '.'; hLine2.Marker = '.';
      xlabel('eigenaxis distance (nm)')
      set(ax(1),'xlim',[xdiameters(1) xdiameters(end)]);
      set(ax(2),'xlim',[xdiameters(1) xdiameters(end)]);
    end
    
    pause
  end
  
  fprintf(1,'seed %d of %d in %.4f s\n',seed,nseeds,(now-t)*86400);
end

if ncuts > 0
  save(outcutfile, 'cut_n', 'cut_d');
end

  
function plot_pts_surf(faces, pts, sel)

if isempty(sel), sel = true(size(pts,1),1); end
x = pts(:,1); y = pts(:,2); z = pts(:,3);

figure(1234); clf
plot3( x(sel), y(sel), z(sel), '.b' ); hold on
plot3( x(~sel), y(~sel), z(~sel), '.r' ); hold on
h = trisurf(faces, x, y, z);
%set(h,'edgecolor','none','facecolor','g','facealpha',0.8);
set( h, 'FaceColor', 'g', 'EdgeColor', 'none', 'facealpha', 0.5);
%view( -70, 40 );
view( 90, 0 );
axis vis3d equal; camlight; lighting phong;
xlabel('x'); ylabel('y'); zlabel('z');
